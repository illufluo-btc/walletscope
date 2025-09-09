#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
WalletScope (MVP, Infura-only friendly, original)
- 输入：EVM 地址（Ethereum mainnet）
- 输出：
  1) 持仓（ETH + ERC20，现价估值）
  2) 最近 50 次交互（合约/类型/时间, Asia/Tokyo）
  3) LLM 总结（目的推断 + 画像标签 + 数据质量）
- 产物：
  out/<address>.summary.json
  out/<address>.actions.csv
"""

import os, sys, csv, json, time, pathlib
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from web3 import Web3
from pydantic import BaseModel, Field

# ---------- ENV ----------
load_dotenv()
ETHERSCAN_API_KEY = (os.getenv("ETHERSCAN_API_KEY") or "").strip()
INFURA_URL        = (os.getenv("INFURA_URL") or "").strip()
INFURA_PROJECT_ID = (os.getenv("INFURA_PROJECT_ID") or "").strip()
DEEPSEEK_API_KEY  = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
OPENAI_BASE_URL   = (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com").strip()
LLM_MODEL         = (os.getenv("LLM_MODEL") or "deepseek-chat").strip()

if not ETHERSCAN_API_KEY:
    print("ERROR: missing ETHERSCAN_API_KEY"); sys.exit(1)
if not (INFURA_URL or INFURA_PROJECT_ID):
    print("ERROR: set INFURA_URL or INFURA_PROJECT_ID in .env"); sys.exit(1)
if not DEEPSEEK_API_KEY:
    print("ERROR: missing DEEPSEEK_API_KEY"); sys.exit(1)

RPC_URL = INFURA_URL or f"https://mainnet.infura.io/v3/{INFURA_PROJECT_ID}"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# ---------- CONST ----------
OUT_DIR = pathlib.Path("out"); OUT_DIR.mkdir(exist_ok=True)
JST = timezone(timedelta(hours=9))
ETHERSCAN_API = "https://api.etherscan.io/api"
FOURBYTE_API  = "https://www.4byte.directory/api/v1/signatures/"
DEFILLAMA_PRICE = "https://coins.llama.fi/prices/current/"

KNOWN_PROTOCOLS = {
    Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"): "UniswapV2Router02",
    Web3.to_checksum_address("0xE592427A0AEce92De3Edee1F18E0157C05861564"): "UniswapV3SwapRouter",
    Web3.to_checksum_address("0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B"): "UniswapUniversalRouter",
    Web3.to_checksum_address("0x3d9819210A31b4961b30EF54bE2aeD79B9c9Cd3B"): "CompoundV2Comptroller",
}

ERC20_MIN_ABI = [
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"name":"owner","type":"address"}],"stateMutability":"view","type":"function"},
]

# ---------- HTTP / RPC ----------
def http_get(url: str, params: Dict[str, Any] | None = None, retry: int = 3, backoff: float = 0.6):
    for i in range(retry):
        r = requests.get(url, params=params, timeout=25)
        if r.ok:
            try:
                return r.json()
            except Exception:
                return r.text
        time.sleep(backoff*(i+1))
    raise RuntimeError(f"GET failed {url} {params}")

def rpc(method: str, params: list):
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for i in range(3):
        r = requests.post(RPC_URL, json=payload, timeout=25)
        if r.ok:
            return r.json().get("result")
        time.sleep(0.4*(i+1))
    raise RuntimeError(f"RPC failed {method} {params}")

def jst_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=JST).isoformat()

# ---------- Etherscan pulls ----------
def get_txlist(address: str, n: int = 50) -> List[Dict[str, Any]]:
    q = {
        "module":"account","action":"txlist","address":address,
        "startblock":0,"endblock":99999999,"page":1,"offset":n,"sort":"desc",
        "apikey":ETHERSCAN_API_KEY
    }
    data = http_get(ETHERSCAN_API, q)
    if isinstance(data, dict) and data.get("status") == "1":
        return data["result"]
    return []

def get_tokentx(address: str, n: int = 300) -> List[Dict[str, Any]]:
    q = {"module":"account","action":"tokentx","address":address,"page":1,"offset":n,"sort":"desc","apikey":ETHERSCAN_API_KEY}
    data = http_get(ETHERSCAN_API, q)
    if isinstance(data, dict) and data.get("status") == "1":
        return data["result"]
    return []

# ---------- Holdings (Infura-only path) ----------
def eth_balance(address: str) -> int:
    res = rpc("eth_getBalance", [Web3.to_checksum_address(address), "latest"])
    return int(res, 16) if res else 0

def discover_token_contracts(logs: List[Dict[str, Any]], limit: int = 80) -> List[str]:
    seen: dict[str, bool] = {}
    for ev in logs:
        ca = ev.get("contractAddress")
        if ca:
            seen[Web3.to_checksum_address(ca)] = True
            if len(seen) >= limit:
                break
    return list(seen.keys())

def fetch_erc20_snapshot(holder: str, contracts: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    hchk = Web3.to_checksum_address(holder)
    for ca in contracts:
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(ca), abi=ERC20_MIN_ABI)
            bal = int(c.functions.balanceOf(hchk).call())
            if bal == 0:
                continue
            try:
                sym = c.functions.symbol().call()
            except Exception:
                sym = None
            try:
                dec = int(c.functions.decimals().call())
            except Exception:
                dec = 18
            out.append({"contract": ca, "balance_raw": bal, "symbol": sym, "decimals": dec})
        except Exception:
            continue
    return out

# ---------- Signatures & Action guess ----------
_SIG_CACHE: dict[str, Optional[str]] = {}
def sig_text(sig4: Optional[str]) -> Optional[str]:
    if not sig4 or sig4 == "0x" or len(sig4) < 10:
        return None
    key = sig4[:10].lower()
    if key in _SIG_CACHE:
        return _SIG_CACHE[key]
    data = http_get(FOURBYTE_API, {"hex_signature": key})
    text = None
    try:
        arr = data.get("results", [])
        if arr:
            text = arr[0].get("text_signature")
    except Exception:
        text = None
    _SIG_CACHE[key] = text
    return text

def guess_action(method_text: Optional[str], eth_value_wei: int) -> str:
    if method_text:
        low = method_text.lower()
        if "approve(" in low: return "approve"
        if "swap" in low: return "swap"
        if "deposit" in low or "supply" in low or "addliquidity" in low: return "deposit"
        if "withdraw" in low or "removeliquidity" in low: return "withdraw"
        if "borrow(" in low: return "borrow"
        if "repay(" in low: return "repay"
        return "contract_call"
    return "eth_transfer" if eth_value_wei > 0 else "unknown"

# ---------- Pricing ----------
def get_prices(contracts: List[str]) -> Dict[str, float]:
    if not contracts:
        prices = {}
    else:
        keys = [f"ethereum:{c.lower()}" for c in contracts]
        prices: Dict[str,float] = {}
        CHUNK = 80
        for i in range(0, len(keys), CHUNK):
            part = keys[i:i+CHUNK]
            data = http_get(DEFILLAMA_PRICE + ",".join(part))
            coins = (data or {}).get("coins", {})
            for k, v in coins.items():
                p = v.get("price")
                if p is not None:
                    prices[k] = float(p)
    # ETH
    try:
        ethj = http_get(DEFILLAMA_PRICE + "coingecko:ethereum")
        ep = ethj.get("coins", {}).get("coingecko:ethereum", {}).get("price")
        if ep is not None:
            prices["eth"] = float(ep)
    except Exception:
        pass
    return prices

# ---------- LLM (DeepSeek, enhanced analysis) ----------

def llm_summary(facts: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{OPENAI_BASE_URL.rstrip('/')}/v1/chat/completions"
    sys_prompt = (
        "你是专业的区块链地址分析师，擅长从链上行为推断用户的交易策略和意图。"
        "分析用户的持仓和最近交易行为，深入推断其可能的投资策略、风险偏好和用户类型。"
        "请用中文回答，输出格式要求 JSON，包含以下字段：\n"
        "1. trading_strategy_analysis: 详细分析用户的交易策略和操作目的\n"
        "2. user_profile: 详细的用户画像描述（不要单一标签，要有深度分析）\n"
        "3. risk_assessment: 风险评估和投资行为特征\n"
        "4. data_insights: 从数据中得出的关键洞察\n"
    )
    user_prompt = (
        "请分析以下钱包地址的链上数据，深入解读用户的交易行为和投资策略：\n\n"
        + json.dumps(facts, ensure_ascii=False, indent=2) + 
        "\n\n请特别关注：\n"
        "1. 交易模式和频率\n"
        "2. 持仓结构和风险偏好\n"
        "3. 与DeFi协议的交互方式\n"
        "4. 可能的套利、借贷、流动性挖矿等策略\n"
        "5. 投资风格（长期持有vs频繁交易）"
    )
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role":"system","content":sys_prompt},
            {"role":"user","content":user_prompt}
        ]
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}

    for attempt in range(2):
        r = requests.post(url, json=payload, headers=headers, timeout=60)
        if not r.ok:
            if attempt == 0:
                time.sleep(0.6); continue
            return {"trading_strategy_analysis":"API请求失败，无法获取分析结果",
                    "user_profile":"数据不足，无法生成用户画像",
                    "risk_assessment":"无法评估",
                    "data_insights":"数据获取失败"}
        try:
            content = r.json()["choices"][0]["message"]["content"].strip()
            parsed = json.loads(content)
            return parsed
        except Exception as e:
            if attempt == 0:
                payload["messages"][-1]["content"] = "【请严格按JSON格式返回，包含trading_strategy_analysis, user_profile, risk_assessment, data_insights字段】\n\n数据：\n" + json.dumps(facts, ensure_ascii=False, indent=2)
                continue
            return {"trading_strategy_analysis":f"LLM解析失败: {str(e)}",
                    "user_profile":"解析错误，无法生成画像",
                    "risk_assessment":"无法分析",
                    "data_insights":"数据解析失败"}

# ---------- MAIN ----------
def main():
    if len(sys.argv) < 2:
        print("Usage: python walletscope.py 0xYourAddress"); sys.exit(1)
    addr = sys.argv[1].strip()
    try:
        chk = Web3.to_checksum_address(addr)
    except Exception:
        print("ERROR: invalid EVM address"); sys.exit(1)
    
    print(f"\n=== 正在分析钱包地址: {chk} ===")
    print("⏳ 获取账户信息...")

    # 账户类型
    code = w3.eth.get_code(chk)
    acct_type = "EOA" if len(code) == 0 else "Contract"

    # 最近交易 / 代币转账
    print("⏳ 获取交易历史...")
    txs = get_txlist(chk, n=20)  # 改为20次交易
    tok = get_tokentx(chk, n=100)  # 减少代币转账查询数量

    # 组装动作
    tok_by_hash: Dict[str, List[Dict[str, Any]]] = {}
    for ev in tok:
        tok_by_hash.setdefault((ev.get("hash") or "").lower(), []).append(ev)

    actions: List[Dict[str, Any]] = []
    for t in txs:
        h = (t.get("hash") or "").lower()
        ts = int(t.get("timeStamp","0"))
        to = t.get("to") or ""
        frm= t.get("from") or ""
        val= int(t.get("value","0"))
        inp= t.get("input") or "0x"
        sig4 = inp[:10].lower() if inp and inp!="0x" else None
        sigtxt = sig_text(sig4)

        proto = None
        try:
            if to:
                cto = Web3.to_checksum_address(to)
                if cto in KNOWN_PROTOCOLS: proto = KNOWN_PROTOCOLS[cto]
        except Exception:
            pass

        tguess = guess_action(sigtxt, val)
        erc20_in, erc20_out = [], []
        for ev in tok_by_hash.get(h, []):
            dec = int(ev.get("tokenDecimal","0") or "0")
            item = {"contract": ev.get("contractAddress"), "symbol": ev.get("tokenSymbol"),
                    "value_raw": ev.get("value"), "decimals": dec}
            if (ev.get("to") or "").lower() == chk.lower():
                erc20_in.append(item)
            if (ev.get("from") or "").lower() == chk.lower():
                erc20_out.append(item)

        actions.append({
            "ts": jst_iso(ts),
            "hash": t.get("hash"),
            "from": frm, "to": to,
            "protocol_hint": proto,
            "method_sig": sig4, "method_name": sigtxt,
            "type_guess": tguess,
            "eth_value_wei": str(val),
            "erc20_in": erc20_in, "erc20_out": erc20_out
        })

    # 持仓：ETH + ERC20（用 tokentx 发现代币集合，再走 Infura balanceOf）
    print("⏳ 分析持仓...")
    eth_bal = eth_balance(chk) / 1e18
    contracts = discover_token_contracts(tok, limit=50)  # 减少合约数量
    erc20_snap = fetch_erc20_snapshot(chk, contracts)

    # 价格
    print("⏳ 获取价格数据...")
    prices = get_prices([x["contract"] for x in erc20_snap])
    eth_price = prices.get("eth")

    holdings: List[Dict[str, Any]] = []
    total_usd = 0.0

    eth_usd = eth_bal * eth_price if eth_price is not None else None
    if eth_usd is not None: total_usd += eth_usd
    holdings.append({"symbol":"ETH","contract":None,"decimals":18,"balance":round(eth_bal,8),
                     "usd": round(eth_usd,2) if eth_usd is not None else None})

    for it in erc20_snap:
        dec = int(it.get("decimals") or 18)
        amt = int(it["balance_raw"]) / (10**dec)
        px  = prices.get(f"ethereum:{it['contract'].lower()}")
        usd = amt * px if px is not None else None
        if usd is not None: total_usd += usd
        holdings.append({
            "symbol": it.get("symbol") or "ERC20",
            "contract": it["contract"],
            "decimals": dec,
            "balance": float(f"{amt:.8f}"),
            "usd": round(usd,2) if usd is not None else None
        })

    holdings_sorted = sorted(holdings, key=lambda x: (x["usd"] or 0.0), reverse=True)
    top10 = holdings_sorted[:10]

    # 供 LLM 的精简事实
    unknown_calls = sum(1 for a in actions if a["type_guess"] in ("unknown","contract_call") and not a["method_name"])
    approvals = sum(1 for a in actions if a["type_guess"] == "approve")
    swaps     = sum(1 for a in actions if a["type_guess"] == "swap")
    uniq_protocols = len({a["protocol_hint"] for a in actions if a["protocol_hint"]})

    compact_actions = [{
        "ts": a["ts"], "hash": a["hash"], "to": a["to"],
        "protocol": a["protocol_hint"], "type": a["type_guess"],
        "method": a["method_name"], "erc20_in_cnt": len(a["erc20_in"]), "erc20_out_cnt": len(a["erc20_out"])
    } for a in actions[:30]]

    facts = {
        "account": {"address": chk, "type": "EOA" if len(w3.eth.get_code(chk))==0 else "Contract"},
        "holdings_top10": top10,
        "actions_lastN": compact_actions,
        "features_min": {
            "unknown_calls": unknown_calls,
            "approvals": approvals,
            "swaps": swaps,
            "unique_protocols": uniq_protocols
        }
    }

    # LLM 分析
    print("🤖 AI 深度分析中...")
    llm = llm_summary(facts)

    # 输出
    out_json = OUT_DIR / f"{chk}.summary.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "account_profile": {"address": chk, "type": facts["account"]["type"]},
            "holdings": holdings_sorted,
            "recent_transactions": actions,
            "portfolio_analysis": {
                "net_worth_usd": round(total_usd,2),
                "transaction_stats": {
                    "total_transactions": len(actions),
                    "approvals": approvals, 
                    "swaps": swaps, 
                    "unique_protocols": uniq_protocols,
                    "unknown_calls": unknown_calls
                }
            },
            "ai_analysis": llm
        }, f, ensure_ascii=False, indent=2)

    out_csv = OUT_DIR / f"{chk}.actions.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts","hash","from","to","protocol_hint","type_guess","method_name",
                    "eth_value_wei","erc20_in_count","erc20_out_count"])
        for a in actions:
            w.writerow([a["ts"], a["hash"], a["from"], a["to"], a.get("protocol_hint") or "",
                        a["type_guess"], a.get("method_name") or "", a["eth_value_wei"],
                        len(a["erc20_in"]), len(a["erc20_out"])])

    print("\n" + "="*60)
    print(f"📊 WalletScope 专业分析报告")
    print("="*60)
    print(f"🏷️  地址: {chk}")
    print(f"🔧 账户类型: {facts['account']['type']}")
    print(f"💰 估算净资产: ${total_usd:.2f}" if total_usd > 0 else "💰 估算净资产: 未知")
    
    print(f"\n📈 持仓分析 (前{min(len(top10), 5)}项):")
    for i, h in enumerate(top10[:5], 1):
        show = f"${h['usd']:.2f}" if h['usd'] is not None else "价格未知"
        print(f"  {i}. {h['symbol']}: {h['balance']} (~{show})")
    
    print(f"\n📋 最近交易活动: {len(actions)}笔交易")
    print("最新5笔交易:")
    for i, action in enumerate(actions[:5], 1):
        time_str = action['ts'][:16].replace('T', ' ')
        contract_info = action['to'][:10] + '...' if action['to'] else '未知'
        action_type = action['type_guess']
        protocol = action.get('protocol_hint', '未知协议')
        print(f"  {i}. {time_str} | {action_type} | {protocol} | {contract_info}")
    
    print(f"\n🤖 AI 深度分析:")
    print("-" * 40)
    
    if llm.get("trading_strategy_analysis"):
        print(f"💡 交易策略分析:")
        print(f"   {llm['trading_strategy_analysis']}")
        print()
    
    if llm.get("user_profile"):
        print(f"👤 用户画像:")
        print(f"   {llm['user_profile']}")
        print()
        
    if llm.get("risk_assessment"):
        print(f"⚠️  风险评估:")
        print(f"   {llm['risk_assessment']}")
        print()
        
    if llm.get("data_insights"):
        print(f"📊 关键洞察:")
        print(f"   {llm['data_insights']}")
    
    print(f"\n📁 详细报告已保存:")
    print(f"   JSON: {out_json}")
    print(f"   CSV:  {out_csv}")
    print("\n✅ 分析完成!")

if __name__ == "__main__":
    sys.exit(main())
