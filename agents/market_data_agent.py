"""
agents/market_data_agent.py — 大盤資料收集 Agent
Yahoo Finance（9 支標的）+ CoinGecko（BTC/ETH/SOL）並行抓取
原本順序執行 ~2 分鐘 → 並行後 ~15 秒
"""

import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from shared.config import MARKET_SYMBOLS, TAIPEI_TZ
from shared.utils import is_weekend, now_tw


# ── Yahoo Finance ─────────────────────────────────────────────────

def fetch_yahoo(symbol: str, name: str = "") -> dict:
    """抓取 Yahoo Finance 單一標的報價"""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?interval=1d&range=5d"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            return _empty_quote(name or symbol)

        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        timestamps = result.get("timestamp", [])
        closes = [c for c in closes if c is not None]
        meta = result.get("meta", {})
        currency = meta.get("currency", "")

        # 判斷是否為週末舊數據
        stale = False
        if timestamps and is_weekend():
            last_dt = datetime.datetime.fromtimestamp(timestamps[-1], tz=TAIPEI_TZ)
            if last_dt.weekday() >= 5 or (now_tw() - last_dt).days >= 1:
                stale = True

        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg = curr - prev
            pct = chg / prev * 100
            emoji = "🔴" if pct < 0 else "🟢"
            return {
                "name": name or symbol,
                "price": f"{curr:,.2f}",
                "change": f"{chg:+.2f}",
                "pct": f"{pct:+.2f}%",
                "emoji": emoji,
                "currency": currency,
                "stale": stale,
            }
        return _empty_quote(name or symbol)
    except Exception as e:
        print(f"Yahoo 錯誤 {symbol}：{e}")
        return _empty_quote(name or symbol)

def _empty_quote(name: str) -> dict:
    return {"name": name, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}


# ── CoinGecko ─────────────────────────────────────────────────────

def fetch_crypto() -> dict:
    """抓取 BTC / ETH / SOL 報價"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin,ethereum,solana",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        results = {}
        for symbol, cg_id in {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.items():
            if cg_id in data:
                price = data[cg_id]["usd"]
                pct   = data[cg_id].get("usd_24h_change", 0) or 0
                emoji = "🔴" if pct < 0 else "🟢"
                results[symbol] = {
                    "price": f"${price:,.2f}",
                    "pct":   f"{pct:+.2f}%",
                    "emoji": emoji,
                }
        return results
    except Exception as e:
        print(f"CoinGecko 錯誤：{e}")
        return {}


# ── Agent ─────────────────────────────────────────────────────────

class MarketDataAgent:
    """
    並行抓取所有大盤指標 + 加密貨幣
    回傳 market_data dict，格式與 stock_daily.py 原版相同
    """

    def run(self) -> dict:
        print("📊 [MarketDataAgent] 並行抓取大盤數據...")
        market_data: dict = {}

        with ThreadPoolExecutor(max_workers=10) as executor:
            # 提交 9 支 Yahoo 標的
            future_to_key = {
                executor.submit(fetch_yahoo, sym, name): key
                for key, (sym, name) in MARKET_SYMBOLS.items()
            }
            # 同時提交加密貨幣
            future_crypto = executor.submit(fetch_crypto)

            # 收集大盤結果
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    market_data[key] = future.result()
                    name = MARKET_SYMBOLS[key][1]
                    from shared.utils import fmt_quote
                    print(f"  ✅ {name}: {fmt_quote(market_data[key])}")
                except Exception as e:
                    print(f"  ❌ {key} 失敗：{e}")
                    market_data[key] = _empty_quote(key)

            # 收集加密貨幣結果
            try:
                market_data["crypto"] = future_crypto.result()
                for sym, d in market_data["crypto"].items():
                    print(f"  ✅ {sym}: {d['emoji']} {d['price']} ({d['pct']})")
            except Exception as e:
                print(f"  ❌ 加密貨幣失敗：{e}")
                market_data["crypto"] = {}

        print(f"📊 [MarketDataAgent] 完成（{len(market_data) - 1} 個指標 + 加密貨幣）")
        return market_data
