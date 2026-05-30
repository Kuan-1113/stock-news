"""
agents/market_data_agent.py — 大盤資料收集 Agent
Yahoo Finance（9 支標的）+ CoinGecko（BTC/ETH/SOL）並行抓取
原本順序執行 ~2 分鐘 → 並行後 ~15 秒

TWII 額外以 range=3mo 抓取 OHLCV，
並用 technical_indicators.py 的純 Python 函式計算 MA/MACD/RSI/KDJ/量比。
（不依賴 yfinance，GitHub Actions 不需額外安裝）
"""

import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from shared.config import MARKET_SYMBOLS, TAIPEI_TZ
from shared.utils import is_weekend, now_tw

# 純 Python 技術指標計算（不需 yfinance）
from technical_indicators import (
    calc_macd, calc_rsi, calc_kdj, calc_bias, calc_volume,
)


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


# ── Yahoo Finance（3 個月歷史 + 技術指標）────────────────────────────

def fetch_yahoo_extended(symbol: str, name: str = "") -> dict:
    """
    抓取 Yahoo Finance 3 個月 OHLCV，並在本地計算技術指標。
    回傳格式與 fetch_yahoo 相同，額外含 "tech" key：
      tech = {available, price, ma5/10/20/60, macd, rsi, kdj, bias, volume}
    """
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?interval=1d&range=3mo"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200:
            q = _empty_quote(name or symbol)
            q["tech"] = {"available": False}
            return q

        data   = r.json()
        result = data["chart"]["result"][0]
        qdata  = result["indicators"]["quote"][0]
        timestamps = result.get("timestamp", [])
        meta   = result.get("meta", {})
        currency = meta.get("currency", "")

        raw_closes  = qdata.get("close",  [])
        raw_highs   = qdata.get("high",   [])
        raw_lows    = qdata.get("low",    [])
        raw_volumes = qdata.get("volume", [])

        # 只保留 close/high/low 都有值的 bar
        valid = [
            (c, h, l, v)
            for c, h, l, v in zip(raw_closes, raw_highs, raw_lows, raw_volumes)
            if c is not None and h is not None and l is not None
        ]
        if len(valid) < 2:
            q = _empty_quote(name or symbol)
            q["tech"] = {"available": False}
            return q

        closes  = [x[0] for x in valid]
        highs   = [x[1] for x in valid]
        lows    = [x[2] for x in valid]
        volumes = [x[3] if x[3] is not None else 0 for x in valid]

        # 週末舊數據判斷
        stale = False
        if timestamps and is_weekend():
            last_dt = datetime.datetime.fromtimestamp(timestamps[-1], tz=TAIPEI_TZ)
            if last_dt.weekday() >= 5 or (now_tw() - last_dt).days >= 1:
                stale = True

        # 標準報價（最後兩根）
        prev, curr = closes[-2], closes[-1]
        chg = curr - prev
        pct = chg / prev * 100
        emoji = "🔴" if pct < 0 else "🟢"

        quote = {
            "name":     name or symbol,
            "price":    f"{curr:,.2f}",
            "change":   f"{chg:+.2f}",
            "pct":      f"{pct:+.2f}%",
            "emoji":    emoji,
            "currency": currency,
            "stale":    stale,
        }

        # ── 技術指標計算（純 Python，不需 yfinance）──────────────────
        tech: dict = {"available": len(closes) >= 20}
        if tech["available"]:
            def _ma(n: int):
                return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

            tech["price"] = round(curr, 2)
            tech["ma5"]   = _ma(5)
            tech["ma10"]  = _ma(10)
            tech["ma20"]  = _ma(20)
            tech["ma60"]  = _ma(60) if len(closes) >= 30 else None

            for vs_key, ma_key in [("ma5_vs", "ma5"), ("ma20_vs", "ma20"), ("ma60_vs", "ma60")]:
                v = tech.get(ma_key)
                tech[vs_key] = ("多頭" if curr >= v else "空頭") if v else "N/A"

            tech["macd"]   = calc_macd(closes)
            tech["rsi"]    = calc_rsi(closes)
            tech["kdj"]    = calc_kdj(highs, lows, closes)
            tech["bias"]   = calc_bias(closes)
            tech["volume"] = calc_volume(volumes)

        quote["tech"] = tech
        return quote

    except Exception as e:
        print(f"Yahoo Extended 錯誤 {symbol}：{e}")
        q = _empty_quote(name or symbol)
        q["tech"] = {"available": False}
        return q


def format_tech_for_prompt(tech: dict) -> str:
    """
    將 fetch_yahoo_extended 計算出的 tech dict，
    格式化為 Claude prompt 用的純文字（無 Markdown）。
    """
    if not tech or not tech.get("available"):
        return "（技術指標資料不足，請以新聞為主要分析依據）"

    lines = []
    curr = tech.get("price", 0)

    # 均線
    ma_parts = []
    for p, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        v = tech.get(key)
        if v:
            pos = "上方" if curr >= v else "下方"
            ma_parts.append(f"MA{p}={v}（現價{pos}）")
    if ma_parts:
        lines.append(f"均線：{' | '.join(ma_parts)}")
        lines.append(
            f"短均（MA5）：{tech.get('ma5_vs','N/A')} | "
            f"中均（MA20）：{tech.get('ma20_vs','N/A')} | "
            f"長均（MA60）：{tech.get('ma60_vs','N/A')}"
        )

    # MACD
    m = tech.get("macd", {})
    if m and m.get("macd") is not None:
        cross = ""
        if m.get("golden_cross"):
            cross = " ★黃金交叉"
        elif m.get("dead_cross"):
            cross = " ★死亡交叉"
        lines.append(
            f"MACD={m['macd']} Signal={m['signal']} 柱狀={m['hist']} "
            f"趨勢：{m['trend']}{cross}"
        )

    # RSI
    r = tech.get("rsi", {})
    if r and r.get("rsi") is not None:
        lines.append(f"RSI(14)={r['rsi']} — {r['signal']}")

    # KDJ
    k = tech.get("kdj", {})
    if k and k.get("k") is not None:
        lines.append(f"KDJ：K={k['k']} D={k['d']} J={k['j']} — {k['signal']}")

    # 乖離率
    b = tech.get("bias", {})
    bp = [f"{p}日={b[f'bias{p}']:+.2f}%" for p in [5, 10, 20] if b and b.get(f"bias{p}") is not None]
    if bp:
        lines.append(f"乖離率：{' | '.join(bp)}")

    # 成交量
    v = tech.get("volume", {})
    if v and v.get("vol_ratio") is not None:
        lines.append(f"量比={v['vol_ratio']}x — {v['vol_trend']}")

    return "\n".join(lines) if lines else "（無法計算技術指標）"


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
            # 提交 Yahoo 標的：TWII 使用 3mo 延伸抓取（含技術指標），其餘標準抓取
            future_to_key = {}
            for key, (sym, name) in MARKET_SYMBOLS.items():
                if key == "twii":
                    future_to_key[executor.submit(fetch_yahoo_extended, sym, name)] = key
                else:
                    future_to_key[executor.submit(fetch_yahoo, sym, name)] = key
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
