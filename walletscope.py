
import os, sys, csv, json, time, pathlib
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from web3 import Web3
from pydantic import BaseModel, Field

# ---------- ENV ----------
load_dotenv()

# Common
DEEPSEEK_API_KEY = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com").strip()
LLM_MODEL = (os.getenv("LLM_MODEL") or "deepseek-chat").strip()
MAX_TX_PER_CHAIN = int(os.getenv("MAX_TX_PER_CHAIN") or "50")

# Ethereum
ETHERSCAN_API_KEY = (os.getenv("ETHERSCAN_API_KEY") or "").strip()
INFURA_URL = (os.getenv("INFURA_URL") or "").strip()
INFURA_PROJECT_ID = (os.getenv("INFURA_PROJECT_ID") or "").strip()

# BSC
BSCSCAN_API_KEY = (os.getenv("BSCSCAN_API_KEY") or "").strip()
BSC_RPC_URL = (os.getenv("BSC_RPC_URL") or "").strip()

# Solana
HELIUS_API_KEY = (os.getenv("HELIUS_API_KEY") or "").strip()
HELIUS_BASE_URL = (os.getenv("HELIUS_BASE_URL") or "").strip()

# Validation
if not DEEPSEEK_API_KEY:
    print("ERROR: missing DEEPSEEK_API_KEY"); sys.exit(1)

# Setup Web3 connections
eth_w3 = None
bsc_w3 = None

if INFURA_URL or INFURA_PROJECT_ID:
    eth_rpc_url = INFURA_URL or f"https://mainnet.infura.io/v3/{INFURA_PROJECT_ID}"
    eth_w3 = Web3(Web3.HTTPProvider(eth_rpc_url))

if BSC_RPC_URL:
    bsc_w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL))

# ---------- CONST ----------
OUT_DIR = pathlib.Path("out"); OUT_DIR.mkdir(exist_ok=True)
JST = timezone(timedelta(hours=9))

# APIs
ETHERSCAN_API = "https://api.etherscan.io/api"
FOURBYTE_API = "https://www.4byte.directory/api/v1/signatures/"
DEFILLAMA_PRICE = "https://coins.llama.fi/prices/current/"

# Chain IDs for Etherscan API v2 multi-chain support
CHAIN_IDS = {
    "eth": "1",      # Ethereum Mainnet
    "bsc": "56",     # BSC Mainnet
}

# Protocol mapping for different chains
ETH_PROTOCOLS = {
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D": "UniswapV2Router02",
    "0xE592427A0AEce92De3Edee1F18E0157C05861564": "UniswapV3SwapRouter", 
    "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B": "UniswapUniversalRouter",
    "0x3d9819210A31b4961b30EF54bE2aeD79B9c9Cd3B": "CompoundV2Comptroller",
}

BSC_PROTOCOLS = {
    "0x10ED43C718714eb63d5aA57B78B54704E256024E": "PancakeSwapV2Router",
    "0x1b81D678ffb9C0263b24A97847620C99d213eB14": "PancakeSwapV3Router",
    "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6": "BiswapRouter",
    "0x05fF2B0DB69458A0750badebc4f9e13aDd608C7F": "PancakeSwapMasterChef",
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

def rpc_call(rpc_url: str, method: str, params: list):
    """通用RPC调用函数"""
    payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
    for i in range(3):
        r = requests.post(rpc_url, json=payload, timeout=25)
        if r.ok:
            return r.json().get("result")
        time.sleep(0.4*(i+1))
    raise RuntimeError(f"RPC failed {method} {params}")

def jst_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=JST).isoformat()

# ---------- Multi-chain transaction pulls ----------
def get_multichain_txlist(address: str, chain: str, n: int = 50) -> List[Dict[str, Any]]:
    """获取多链交易列表（使用Etherscan API v2）"""
    if not ETHERSCAN_API_KEY or chain not in CHAIN_IDS:
        return []
    q = {
        "module":"account","action":"txlist","address":address,
        "startblock":0,"endblock":99999999,"page":1,"offset":n,"sort":"desc",
        "chainId":CHAIN_IDS[chain],
        "apikey":ETHERSCAN_API_KEY
    }
    print(f"🔍 调用API: {chain.upper()}, chainId={CHAIN_IDS[chain]}")
    data = http_get(ETHERSCAN_API, q)
    if isinstance(data, dict):
        if data.get("status") == "1":
            result = data["result"]
            print(f"✅ {chain.upper()}交易数据: 获取到 {len(result)} 笔交易")
            return result
        else:
            print(f"❌ {chain.upper()}交易数据获取失败: {data.get('message', 'Unknown error')}")
    return []

def get_multichain_tokentx(address: str, chain: str, n: int = 300) -> List[Dict[str, Any]]:
    """获取多链代币转账记录（使用Etherscan API v2）"""
    if not ETHERSCAN_API_KEY or chain not in CHAIN_IDS:
        return []
    q = {
        "module":"account","action":"tokentx","address":address,
        "page":1,"offset":n,"sort":"desc",
        "chainId":CHAIN_IDS[chain],
        "apikey":ETHERSCAN_API_KEY
    }
    data = http_get(ETHERSCAN_API, q)
    if isinstance(data, dict):
        if data.get("status") == "1":
            result = data["result"]
            print(f"✅ {chain.upper()}代币数据: 获取到 {len(result)} 笔代币转账")
            return result
        else:
            print(f"❌ {chain.upper()}代币数据获取失败: {data.get('message', 'Unknown error')}")
    return []

# 保持向后兼容的函数名
def get_eth_txlist(address: str, n: int = 50) -> List[Dict[str, Any]]:
    """获取以太坊交易列表（向后兼容）"""
    return get_multichain_txlist(address, "eth", n)

def get_eth_tokentx(address: str, n: int = 300) -> List[Dict[str, Any]]:
    """获取以太坊代币转账记录（向后兼容）"""
    return get_multichain_tokentx(address, "eth", n)

def get_bsc_txlist(address: str, n: int = 50) -> List[Dict[str, Any]]:
    """获取BSC交易列表（向后兼容）"""
    return get_multichain_txlist(address, "bsc", n)

def get_bsc_tokentx(address: str, n: int = 300) -> List[Dict[str, Any]]:
    """获取BSC代币转账记录（向后兼容）"""
    return get_multichain_tokentx(address, "bsc", n)

# ---------- Multi-chain Holdings ----------
def get_chain_balance(address: str, chain: str, w3_instance) -> int:
    """获取指定链上的原生代币余额"""
    if not w3_instance:
        return 0
    try:
        balance = w3_instance.eth.get_balance(Web3.to_checksum_address(address))
        return balance
    except Exception:
        return 0

def discover_token_contracts(logs: List[Dict[str, Any]], limit: int = 80) -> List[str]:
    seen: dict[str, bool] = {}
    for ev in logs:
        ca = ev.get("contractAddress")
        if ca:
            seen[Web3.to_checksum_address(ca)] = True
            if len(seen) >= limit:
                break
    return list(seen.keys())

def fetch_erc20_snapshot(holder: str, contracts: List[str], w3_instance, chain_prefix: str) -> List[Dict[str, Any]]:
    """获取ERC20代币快照"""
    if not w3_instance:
        return []
    out: List[Dict[str, Any]] = []
    hchk = Web3.to_checksum_address(holder)
    for ca in contracts:
        try:
            c = w3_instance.eth.contract(address=Web3.to_checksum_address(ca), abi=ERC20_MIN_ABI)
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
            out.append({"contract": ca, "balance_raw": bal, "symbol": sym, "decimals": dec, "chain": chain_prefix})
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
def get_multichain_prices(contracts_by_chain: Dict[str, List[str]]) -> Dict[str, float]:
    """获取多链代币价格"""
    prices: Dict[str, float] = {}
    all_keys = []
    
    # 构建所有需要查询的代币key
    for chain, contracts in contracts_by_chain.items():
        if chain == "eth":
            chain_prefix = "ethereum"
        elif chain == "bsc":
            chain_prefix = "bsc"
        else:
            continue
            
        for contract in contracts:
            all_keys.append(f"{chain_prefix}:{contract.lower()}")
    
    # 批量查询价格
    if all_keys:
        CHUNK = 80
        for i in range(0, len(all_keys), CHUNK):
            part = all_keys[i:i+CHUNK]
            try:
                data = http_get(DEFILLAMA_PRICE + ",".join(part))
                coins = (data or {}).get("coins", {})
                for k, v in coins.items():
                    p = v.get("price")
                    if p is not None:
                        prices[k] = float(p)
            except Exception:
                continue
    
    # 原生代币价格
    try:
        # ETH价格
        ethj = http_get(DEFILLAMA_PRICE + "coingecko:ethereum")
        ep = ethj.get("coins", {}).get("coingecko:ethereum", {}).get("price")
        if ep is not None:
            prices["eth"] = float(ep)
            
        # BNB价格  
        bnbj = http_get(DEFILLAMA_PRICE + "coingecko:binancecoin")
        bp = bnbj.get("coins", {}).get("coingecko:binancecoin", {}).get("price")
        if bp is not None:
            prices["bnb"] = float(bp)
    except Exception:
        pass
        
    return prices

# ---------- Solana Support ----------
def get_solana_transactions(address: str, limit: int = 50) -> List[Dict[str, Any]]:
    """获取Solana交易记录"""
    if not HELIUS_BASE_URL:
        return []
    
    url = f"{HELIUS_BASE_URL}/v0/addresses/{address}/transactions"
    params = {"limit": limit}
    
    try:
        data = http_get(url, params)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def get_solana_balances(address: str) -> Dict[str, Any]:
    """获取Solana账户余额"""
    if not HELIUS_BASE_URL:
        return {"sol_balance": 0, "tokens": []}
    
    try:
        # 获取SOL余额
        sol_balance = 0
        url = HELIUS_BASE_URL.split('?')[0]  # 去掉query参数
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address]
        }
        headers = {"Content-Type": "application/json"}
        
        r = requests.post(url, json=payload, headers=headers, timeout=25)
        if r.ok:
            result = r.json().get("result")
            if result:
                sol_balance = result.get("value", 0) / 1e9  # 转换为SOL
        
        # 获取代币余额
        tokens = []
        token_url = f"{HELIUS_BASE_URL}/v0/addresses/{address}/balances"
        token_data = http_get(token_url)
        if isinstance(token_data, dict):
            tokens = token_data.get("tokens", [])
        
        return {"sol_balance": sol_balance, "tokens": tokens}
    except Exception:
        return {"sol_balance": 0, "tokens": []}

# ---------- LLM (DeepSeek, enhanced analysis) ----------

def llm_summary(facts: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{OPENAI_BASE_URL.rstrip('/')}/v1/chat/completions"
    sys_prompt = (
    "你是一名区块链地址分析助手。你只能依据“用户提供的事实 JSON”写报告，不得自行检索或臆测链上数据。输出必须是中文自然语言，不要输出 JSON、表格或代码块；不要展示你的思考过程。\n"
    "结构与顺序（若某链不存在则跳过该小节）：\n"
    "1）概览（1–2 句）：地址类型（EOA/合约）、分析覆盖的链（ETH/BSC/SOL）、最近活跃区间、总资产估算（约）。\n"
    "2）ETH 分析（2–5 句）：主要持仓（代币/数量/大致占比）、最近交互类型与涉及协议、可观察到的行为模式。关键断言尽量附 1–2 个“引用锚点”，例如交易哈希后 6 位或协议名（示例：tx …cAfe，UniswapV3）。\n"
    "3）BSC 分析（2–5 句）：同上。\n"
    "4）SOL 分析（2–5 句）：同上（在 SOL 请使用具体 program 名称，如 Jupiter/Orca/Raydium/Solend 等）。\n"
    "5）综合判断（1–3 句）：跨链整体倾向或目的的“审慎推测”，列出 2–3 个证据锚点；若证据不足，明确写“证据不足”。\n"
    "6）注意事项（1–3 句）：数据缺口（例如 unknown_calls 较多、价格为现价非历史价、内部交易缺失、NFT 仅做抽样等）与解读边界。\n"
    "写作风格：专业克制、短句优先、避免口头语；不超过 600 字。\n"
    "不得进行身份推断或现实世界归因；可使用中性概括词（如“可能偏多”“疑似空投参与”），但必须附证据锚点。"
    )
    user_prompt = (
        "【写作要求补充】\n"
        "- 先严格描述事实（持有的 token 种类与数量、近 50 笔交互的类型与涉及协议、是否出现 approve/内部转账/NFT 交互等）。\n"
        "- 在\"综合判断\"里可少量推测，但必须基于前述事实并附证据锚点；没有把握就写\"证据不足\"。\n\n"
        "【事实 JSON】\n"
        + json.dumps(facts, ensure_ascii=False, indent=2) + "\n\n"
        "说明：\n"
        "- facts.chains 为数组，每项形如：\n"
        "  {\n"
        "    \"chain\": \"eth|bsc|sol\",\n"
        "    \"holdings_top10\": [{\"symbol\":\"USDC\",\"contract\":\"0x...\",\"decimals\":6,\"balance\":123.45,\"usd\":123.45}, ...],\n"
        "    \"actions_lastN\": [{\"ts\":\"2025-09-09T12:00:00+09:00\",\"hash\":\"0x...\",\"to\":\"0x...\",\"protocol\":\"UniswapV3\",\"type\":\"swap\",\"method\":\"swapExactTokensForTokens\",\"erc20_in_cnt\":1,\"erc20_out_cnt\":1}, ...],\n"
        "    \"features_min\": {\"unknown_calls\":2,\"approvals\":5,\"swaps\":12,\"unique_protocols\":4}\n"
        "  }\n"
        "- profile: {\"address\":\"<addr>\",\"kind\":\"EOA|Contract\"}。\n"
        "- 交易锚点可用 tx 哈希后 6 位；协议锚点用协议名或合约别名（若存在）。"
    )
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
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
            return {"analysis_report": "API请求失败，无法获取分析结果"}
        try:
            content = r.json()["choices"][0]["message"]["content"].strip()
            return {"analysis_report": content}
        except Exception as e:
            if attempt == 0:
                payload["messages"][-1]["content"] = "【请严格按中文自然语言格式返回分析报告，不要JSON格式】\n\n数据：\n" + json.dumps(facts, ensure_ascii=False, indent=2)
                continue
            return {"analysis_report": f"LLM解析失败: {str(e)}"}

# ---------- MAIN ----------
def analyze_chain_data(address: str, chain: str) -> Dict[str, Any]:
    """分析单个链的数据"""
    chain_data = {
        "chain": chain,
        "holdings_top10": [],
        "actions_lastN": [],
        "features_min": {"unknown_calls": 0, "approvals": 0, "swaps": 0, "unique_protocols": 0}
    }
    
    if chain == "eth" and eth_w3 and ETHERSCAN_API_KEY:
        # 以太坊数据
        chk = Web3.to_checksum_address(address)
        
        # 获取交易
        txs = get_eth_txlist(address, MAX_TX_PER_CHAIN)
        tok = get_eth_tokentx(address, 100)
        
        # 处理持仓
        eth_bal = get_chain_balance(address, "eth", eth_w3) / 1e18
        contracts = discover_token_contracts(tok, limit=50)
        erc20_snap = fetch_erc20_snapshot(address, contracts, eth_w3, "eth")
        
        # 处理交易动作
        actions = process_evm_transactions(txs, tok, chk, ETH_PROTOCOLS)
        
        chain_data["actions_lastN"] = actions[:MAX_TX_PER_CHAIN]
        chain_data["features_min"] = calculate_features(actions)
        
        # 持仓数据
        holdings = []
        if eth_bal > 0:
            holdings.append({"symbol": "ETH", "contract": None, "decimals": 18, "balance": round(eth_bal, 8), "chain": "eth"})
        
        for item in erc20_snap:
            dec = item.get("decimals", 18)
            amt = item["balance_raw"] / (10**dec)
            if amt > 0:
                holdings.append({
                    "symbol": item.get("symbol", "UNKNOWN"),
                    "contract": item["contract"],
                    "decimals": dec,
                    "balance": float(f"{amt:.8f}"),
                    "chain": "eth"
                })
        
        chain_data["holdings_top10"] = holdings[:10]
        
    elif chain == "bsc" and bsc_w3 and ETHERSCAN_API_KEY:
        # BSC数据
        chk = Web3.to_checksum_address(address)
        
        txs = get_bsc_txlist(address, MAX_TX_PER_CHAIN)
        tok = get_bsc_tokentx(address, 100)
        
        bnb_bal = get_chain_balance(address, "bsc", bsc_w3) / 1e18
        contracts = discover_token_contracts(tok, limit=50)
        erc20_snap = fetch_erc20_snapshot(address, contracts, bsc_w3, "bsc")
        
        actions = process_evm_transactions(txs, tok, chk, BSC_PROTOCOLS)
        
        chain_data["actions_lastN"] = actions[:MAX_TX_PER_CHAIN]
        chain_data["features_min"] = calculate_features(actions)
        
        holdings = []
        if bnb_bal > 0:
            holdings.append({"symbol": "BNB", "contract": None, "decimals": 18, "balance": round(bnb_bal, 8), "chain": "bsc"})
        
        for item in erc20_snap:
            dec = item.get("decimals", 18)
            amt = item["balance_raw"] / (10**dec)
            if amt > 0:
                holdings.append({
                    "symbol": item.get("symbol", "UNKNOWN"),
                    "contract": item["contract"],
                    "decimals": dec,
                    "balance": float(f"{amt:.8f}"),
                    "chain": "bsc"
                })
        
        chain_data["holdings_top10"] = holdings[:10]
        
    elif chain == "sol" and HELIUS_BASE_URL:
        # Solana数据
        sol_txs = get_solana_transactions(address, MAX_TX_PER_CHAIN)
        sol_balances = get_solana_balances(address)
        
        # 处理Solana交易和持仓
        actions = process_solana_transactions(sol_txs)
        chain_data["actions_lastN"] = actions[:MAX_TX_PER_CHAIN]
        chain_data["features_min"] = calculate_solana_features(actions)
        
        holdings = []
        if sol_balances["sol_balance"] > 0:
            holdings.append({"symbol": "SOL", "contract": None, "decimals": 9, "balance": round(sol_balances["sol_balance"], 8), "chain": "sol"})
        
        for token in sol_balances["tokens"][:10]:
            if token.get("amount", 0) > 0:
                holdings.append({
                    "symbol": token.get("symbol", "UNKNOWN"),
                    "contract": token.get("mint"),
                    "decimals": token.get("decimals", 9),
                    "balance": token.get("amount", 0),
                    "chain": "sol"
                })
        
        chain_data["holdings_top10"] = holdings
    
    return chain_data

def process_evm_transactions(txs: List[Dict], tok: List[Dict], chk: str, protocols: Dict[str, str]) -> List[Dict[str, Any]]:
    """处理EVM链的交易数据"""
    tok_by_hash = {}
    for ev in tok:
        tok_by_hash.setdefault((ev.get("hash") or "").lower(), []).append(ev)

    actions = []
    for t in txs:
        h = (t.get("hash") or "").lower()
        ts = int(t.get("timeStamp", "0"))
        to = t.get("to") or ""
        frm = t.get("from") or ""
        val = int(t.get("value", "0"))
        inp = t.get("input") or "0x"
        sig4 = inp[:10].lower() if inp and inp != "0x" else None
        sigtxt = sig_text(sig4)

        proto = None
        try:
            if to:
                cto = Web3.to_checksum_address(to)
                if cto in protocols:
                    proto = protocols[cto]
        except Exception:
            pass

        tguess = guess_action(sigtxt, val)
        erc20_in, erc20_out = [], []
        for ev in tok_by_hash.get(h, []):
            dec = int(ev.get("tokenDecimal", "0") or "0")
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
            "protocol": proto,
            "type": tguess,
            "method": sigtxt,
            "erc20_in_cnt": len(erc20_in),
            "erc20_out_cnt": len(erc20_out)
        })
    
    return actions

def process_solana_transactions(txs: List[Dict]) -> List[Dict[str, Any]]:
    """处理Solana交易数据"""
    actions = []
    for tx in txs:
        # 这里简化处理Solana交易，实际项目中需要更详细的解析
        actions.append({
            "ts": tx.get("timestamp", ""),
            "hash": tx.get("signature", ""),
            "from": tx.get("feePayer", ""),
            "to": "",
            "protocol": None,
            "type": "sol_transaction",
            "method": None,
            "erc20_in_cnt": 0,
            "erc20_out_cnt": 0
        })
    return actions

def calculate_features(actions: List[Dict]) -> Dict[str, int]:
    """计算交易特征"""
    unknown_calls = sum(1 for a in actions if a["type"] in ("unknown", "contract_call") and not a["method"])
    approvals = sum(1 for a in actions if a["type"] == "approve")
    swaps = sum(1 for a in actions if a["type"] == "swap")
    unique_protocols = len({a["protocol"] for a in actions if a["protocol"]})
    
    return {
        "unknown_calls": unknown_calls,
        "approvals": approvals,
        "swaps": swaps,
        "unique_protocols": unique_protocols
    }

def calculate_solana_features(actions: List[Dict]) -> Dict[str, int]:
    """计算Solana交易特征"""
    return {
        "unknown_calls": 0,
        "approvals": 0,
        "swaps": 0,
        "unique_protocols": 0
    }

def main():
    if len(sys.argv) < 2:
        print("Usage: python walletscope.py <address>"); sys.exit(1)
    
    address = sys.argv[1].strip()
    print(f"\n=== 正在分析钱包地址: {address} ===")
    
    # 多链分析
    chains_to_analyze = []
    if eth_w3 and ETHERSCAN_API_KEY:
        chains_to_analyze.append("eth")
    if bsc_w3 and ETHERSCAN_API_KEY:  # BSC也使用ETHERSCAN_API_KEY
        chains_to_analyze.append("bsc")
    if HELIUS_BASE_URL:
        chains_to_analyze.append("sol")
    
    if not chains_to_analyze:
        print("ERROR: 没有可用的链配置"); sys.exit(1)
    
    print(f"⏳ 支持的链: {', '.join(chains_to_analyze)}")
    if "bsc" in chains_to_analyze:
        print("ℹ️  BSC使用Etherscan API v2 (chainId=56)")
    
    # 分析每个链的数据
    chain_results = []
    for chain in chains_to_analyze:
        print(f"⏳ 分析 {chain.upper()} 链数据...")
        chain_data = analyze_chain_data(address, chain)
        if chain_data["holdings_top10"] or chain_data["actions_lastN"]:
            chain_results.append(chain_data)
    
    if not chain_results:
        print("❌ 未找到任何链上活动")
        return
    
    # 获取价格数据
    print("⏳ 获取价格数据...")
    contracts_by_chain = {}
    all_holdings = []
    
    for chain_data in chain_results:
        chain = chain_data["chain"]
        contracts = [h["contract"] for h in chain_data["holdings_top10"] if h["contract"]]
        if contracts:
            contracts_by_chain[chain] = contracts
        all_holdings.extend(chain_data["holdings_top10"])
    
    prices = get_multichain_prices(contracts_by_chain)
    
    # 计算总价值并添加USD估值
    total_usd = 0.0
    for holding in all_holdings:
        if holding["symbol"] == "ETH" and "eth" in prices:
            holding["usd"] = holding["balance"] * prices["eth"]
        elif holding["symbol"] == "BNB" and "bnb" in prices:
            holding["usd"] = holding["balance"] * prices["bnb"]
        elif holding["contract"]:
            chain = holding.get("chain", "eth")
            chain_prefix = "ethereum" if chain == "eth" else "bsc" if chain == "bsc" else chain
            price_key = f"{chain_prefix}:{holding['contract'].lower()}"
            if price_key in prices:
                holding["usd"] = holding["balance"] * prices[price_key]
            else:
                holding["usd"] = None
        else:
            holding["usd"] = None
        
        if holding["usd"] is not None:
            total_usd += holding["usd"]
    
    # 按价值排序
    all_holdings.sort(key=lambda x: x.get("usd", 0) or 0, reverse=True)
    
    # 准备AI分析数据
    facts = {
        "chains": chain_results,
        "profile": {"address": address, "kind": "EOA"}  # 简化处理
    }
    
    # AI分析
    print("🤖 AI 深度分析中...")
    llm = llm_summary(facts)
    
    # 输出报告
    out_json = OUT_DIR / f"{address}.summary.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "profile": facts["profile"],
            "chains": chain_results,
            "total_net_worth_usd": round(total_usd, 2),
            "ai_analysis": llm
        }, f, ensure_ascii=False, indent=2)
    
    # 生成CSV（合并所有链的交易）
    out_csv = OUT_DIR / f"{address}.actions.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["chain", "ts", "hash", "from", "to", "protocol", "type", "method", "erc20_in_cnt", "erc20_out_cnt"])
        for chain_data in chain_results:
            for action in chain_data["actions_lastN"]:
                w.writerow([
                    chain_data["chain"], action["ts"], action["hash"],
                    action["from"], action["to"], action.get("protocol", ""),
                    action["type"], action.get("method", ""),
                    action["erc20_in_cnt"], action["erc20_out_cnt"]
                ])
    
    # 显示结果
    print("\n" + "="*60)
    print(f"📊 WalletScope 多链分析报告")
    print("="*60)
    print(f"🏷️  地址: {address}")
    print(f"⛓️  支持链: {', '.join([c['chain'].upper() for c in chain_results])}")
    print(f"💰 估算净资产: ${total_usd:.2f}" if total_usd > 0 else "💰 估算净资产: 未知")
    
    print(f"\n📈 跨链持仓分析 (前10项):")
    for i, h in enumerate(all_holdings[:10], 1):
        show = f"${h['usd']:.2f}" if h.get('usd') is not None else "价格未知"
        chain_tag = f"[{h['chain'].upper()}]"
        print(f"  {i}. {chain_tag} {h['symbol']}: {h['balance']} (~{show})")
    
    total_actions = sum(len(c["actions_lastN"]) for c in chain_results)
    print(f"\n📋 跨链交易活动: {total_actions}笔交易")
    
    for chain_data in chain_results:
        if chain_data["actions_lastN"]:
            print(f"\n  {chain_data['chain'].upper()}链 最新3笔交易:")
            for i, action in enumerate(chain_data["actions_lastN"][:3], 1):
                time_str = action['ts'][:16].replace('T', ' ') if action['ts'] else '未知时间'
                contract_info = action['to'][:10] + '...' if action['to'] else '未知'
                action_type = action['type']
                protocol = action.get('protocol', '未知协议')
                print(f"    {i}. {time_str} | {action_type} | {protocol} | {contract_info}")
    
    print(f"\n🤖 AI 深度分析:")
    print("-" * 40)
    
    if llm.get("analysis_report"):
        print(llm['analysis_report'])
    else:
        print("分析报告生成失败")
    
    print(f"\n📁 详细报告已保存:")
    print(f"   JSON: {out_json}")
    print(f"   CSV:  {out_csv}")
    print("\n✅ 多链分析完成!")

if __name__ == "__main__":
    main()
