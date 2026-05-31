"""
weekly_review.py — 每週市場回顧報告

每週五 17:30 台灣時間執行，發送至 DISCORD_TW（或 DISCORD_WATCHLIST）。

報告內容：
  1. 本週大盤表現（TWII + DJI + NASDAQ + S&P500 + VIX）
  2. 自選股本週強弱排行（WATCHLIST）
  3. 策略信號本週命中回顧（signal_db 近 10 日）
  4. Claude 週評摘要

環境變數：
  DISCORD_TW / DISCORD_WATCHLIST — 發送目標
  ANTHROPIC_API_KEY              — Claude 分析
  SIGNAL_DB_PATH                 — 策略 DB（選填）
"""

from __future__ import annotations

import os
import sys
import time
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 確保 package root 在 sys.path ─────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from shared.claude_client import simple_call as claude_call
except ImportError:
    def claude_call(prompt: str, max_tokens: int = 1800, **kw) -> str:  # type: ignore[misc]
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "（無 ANTHROPIC_API_KEY）"
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        return r.json()["content"][0]["text"].strip()

# ── 設定 ─────────────────────────────────────────────────────────

DISCORD_URL = (
    os.environ.get("DISCORD_WEEKLY", "")
    or os.environ.get("DISCORD_TW", "")
    or os.environ.get("DISCORD_WATCHLIST", "")
)

# 本週回顧涵蓋的標的
MARKET_SYMBOLS = [
    ("^TWII",    "台灣加權"),
    ("^DJI",     "道瓊工業"),
    ("^IXIC",    "納斯達克"),
    ("^GSPC",    "S&P 500"),
    ("^VIX",     "VIX 恐慌指數"),
    ("^TNX",     "美債 10Y"),
    ("GC=F",     "黃金"),
    ("CL=F",     "原油"),
]

WATCHLIST = [
    {"symbol": "2330.TW", "name": "台積電"},
    {"symbol": "2454.TW", "name": "聯發科"},
    {"symbol": "2382.TW", "name": "廣達"},
    {"symbol": "NVDA",    "name": "輝達"},
    {"symbol": "AAPL",    "name": "蘋果"},
    {"symbol": "TSLA",    "name": "特斯拉"},
]


# ── 資料抓取 ─────────────────────────────────────────────────────

def fetch_weekly_perf(symbol: str) -> dict | None:
    """
    抓取最近 15 個交易日資料，計算本週漲跌（5 日）。
    回傳：{"name": str, "price": float, "weekly_pct": float, "daily_pct": float}
    """
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "15d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return None
        data   = r.json()
        result = data.get("chart", {}).get("result", [None])[0]
        if not result:
            return None
        meta   = result.get("meta", {})
        closes = [c for c in result["indicators"]["quote"][0].get("close", []) if c is not None]
        if len(closes) < 2:
            return None

        curr     = closes[-1]
        prev_day = closes[-2]
        daily    = (curr - prev_day) / prev_day * 100

        # 本週（5 個交易日）漲跌
        week_base = closes[-6] if len(closes) >= 6 else closes[0]
        weekly    = (curr - week_base) / week_base * 100

        return {
            "name":       meta.get("shortName", symbol),
            "price":      curr,
            "currency":   meta.get("currency", ""),
            "weekly_pct": round(weekly, 2),
            "daily_pct":  round(daily, 2),
        }
    except Exception as e:
        print(f"  ⚠️  fetch_weekly_perf({symbol})：{e}")
        return None


def _parallel_fetch(symbol_list: list) -> dict[str, dict]:
    """並行抓取多個標的的週線資料"""
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_weekly_perf, sym): sym for sym in symbol_list}
        for fut in as_completed(futs):
            sym = futs[fut]
            data = fut.result()
            if data:
                results[sym] = data
            time.sleep(0.05)
    return results


# ── 策略信號命中回顧 ──────────────────────────────────────────────

def get_strategy_hits() -> str:
    """
    從 signal_db 查詢近 10 日的信號結算結果，
    回傳格式化摘要文字。
    """
    try:
        from strategy_agent.signal_db import init_db, _conn
        init_db()
        cutoff = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        with _conn() as c:
            rows = c.execute("""
                SELECT date, symbol, name, strategy, entry_price,
                       result_5d, win_5d, result_10d, win_10d
                FROM signals
                WHERE date >= ?
                ORDER BY date DESC
            """, (cutoff,)).fetchall()

        if not rows:
            return "（近 10 日無策略信號記錄）"

        wins   = sum(1 for r in rows if r["win_5d"] == 1 or r["win_10d"] == 1)
        losses = sum(1 for r in rows if r["win_5d"] == 0 or r["win_10d"] == 0)
        total  = len(rows)
        settled = wins + losses

        lines = [
            f"近 10 日策略信號：{total} 筆",
            f"已結算：{settled} 筆（勝 {wins} / 敗 {losses}"
            + (f"　勝率 {wins/settled*100:.0f}%" if settled else "") + "）",
            "─" * 20,
        ]
        for r in rows[:10]:   # 最多顯示 10 筆
            w5  = "✅" if r["win_5d"]  == 1 else ("❌" if r["win_5d"]  == 0 else "⏳")
            w10 = "✅" if r["win_10d"] == 1 else ("❌" if r["win_10d"] == 0 else "⏳")
            r5  = f"{r['result_5d']:+.1f}%" if r["result_5d"] is not None else "待結"
            r10 = f"{r['result_10d']:+.1f}%" if r["result_10d"] is not None else "待結"
            lines.append(
                f"• {r['date']} {r['symbol']} {r['name']}"
                f"  5d:{w5}{r5}　10d:{w10}{r10}"
                f"  [{r['strategy']}] @{r['entry_price']:.1f}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"（策略信號查詢失敗：{e}）"


# ── 報告生成 ─────────────────────────────────────────────────────

def build_report(
    market_data: dict[str, dict],
    watchlist_data: dict[str, dict],
    strategy_hits: str,
    week_str: str,
) -> str:
    """組合週線回顧報告文字"""

    # ── 大盤表現 ──────────────────────────────────────────────────
    market_lines = []
    for sym, label in MARKET_SYMBOLS:
        d = market_data.get(sym)
        if not d:
            continue
        emoji = "🔴" if d["weekly_pct"] > 0 else ("🟢" if d["weekly_pct"] < 0 else "⬛")
        # 特殊指標（VIX 上升 = 恐慌，用反色）
        if "VIX" in label or "恐慌" in label:
            emoji = "⚠️" if d["weekly_pct"] > 5 else ("📉" if d["weekly_pct"] < -5 else "🟡")
        price_str = f"{d['price']:,.2f}"
        currency  = d.get("currency", "")
        market_lines.append(
            f"• {emoji} **{label}**　{price_str} {currency}"
            f"　週漲跌：**{d['weekly_pct']:+.2f}%**（日：{d['daily_pct']:+.2f}%）"
        )

    # ── 自選股排行 ────────────────────────────────────────────────
    wl_rows = []
    for item in WATCHLIST:
        sym  = item["symbol"]
        name = item["name"]
        d    = watchlist_data.get(sym)
        if d:
            wl_rows.append((d["weekly_pct"], sym, name, d))

    wl_rows.sort(key=lambda x: x[0], reverse=True)
    wl_lines = []
    for pct, sym, name, d in wl_rows:
        medal = "🥇" if pct == max(r[0] for r in wl_rows) else (
                "🔻" if pct == min(r[0] for r in wl_rows) else "•")
        emoji = "▲" if pct > 0 else ("▼" if pct < 0 else "─")
        wl_lines.append(
            f"{medal} **{name}**（{sym}）　"
            f"{emoji} **{pct:+.2f}%** 本週　日：{d['daily_pct']:+.2f}%"
        )

    # ── Claude 週評 ───────────────────────────────────────────────
    market_summary = "\n".join(market_lines) if market_lines else "（無資料）"
    wl_summary     = "\n".join(wl_lines)     if wl_lines     else "（無資料）"

    prompt = f"""你是一位台股市場週評分析師，請以繁體中文撰寫本週市場週評（600字以內）。

【本週（{week_str}）大盤表現】
{market_summary}

【自選股本週漲跌】
{wl_summary}

【策略信號本週命中回顧】
{strategy_hits}

請輸出三節（bullet 格式，禁表格）：

**🌍 本週宏觀市場解讀**
• 本週最重要的 2-3 個市場主題（帶數據）
• VIX / 美債 / 美元的方向研判 + 對台股影響

**🇹🇼 台股週線分析**
• 加權指數本週趨勢 + 成交量能
• 強弱族群（哪類股領漲/領跌？）
• 下週需關注的關鍵支撐/壓力位

**📅 下週前瞻**
• 重要事件/數據（法說會、財報、Fed 說話日等）
• 操作建議：積極/保守/觀望？核心理由一句

> ⚠️ AI 週評，不構成投資建議"""

    print("  🤖 Claude 生成週評...")
    try:
        ai_text = claude_call(prompt, max_tokens=1400)
    except Exception as e:
        ai_text = f"（Claude 週評生成失敗：{e}）"

    # ── 組合最終報告 ──────────────────────────────────────────────
    today = datetime.date.today().strftime("%Y-%m-%d")
    header = f"## 📅 本週市場回顧 | {week_str}（{today} 整理）"

    sections = [
        header,
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "### 🌍 大盤指標本週表現",
        "\n".join(market_lines) if market_lines else "（無資料）",
        "",
        "### 📊 自選股本週強弱排行",
        "\n".join(wl_lines) if wl_lines else "（無資料）",
        "",
        "### 🎯 策略信號本週命中回顧",
        strategy_hits,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        ai_text,
        "",
        "> 📊 資料來源：Yahoo Finance ｜ 策略 Agent Signal DB",
    ]
    return "\n".join(sections)


# ── Discord 發送 ─────────────────────────────────────────────────

def send_discord(content: str) -> bool:
    if not DISCORD_URL:
        print("  ⚠️  未設定 DISCORD_WEEKLY / DISCORD_TW / DISCORD_WATCHLIST，跳過")
        return False
    chunks = [content[i:i+1990] for i in range(0, len(content), 1990)]
    for chunk in chunks:
        try:
            r = requests.post(DISCORD_URL, json={"content": chunk}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"  ❌ Discord 發送失敗：HTTP {r.status_code}")
                return False
            time.sleep(0.5)
        except Exception as e:
            print(f"  ❌ Discord 例外：{e}")
            return False
    return True


# ── 主程式 ────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> str:
    """執行每週回顧報告，回傳報告文字"""
    today     = datetime.date.today()
    # 取本週第一天（週一）到今天
    monday    = today - datetime.timedelta(days=today.weekday())
    week_str  = f"{monday.strftime('%m/%d')}～{today.strftime('%m/%d')}"

    print(f"\n{'='*50}")
    print(f"📅 每週回顧報告  {week_str}")
    print(f"{'='*50}")

    # ── 1. 並行抓市場資料 ─────────────────────────────────────────
    all_symbols = [sym for sym, _ in MARKET_SYMBOLS] + [item["symbol"] for item in WATCHLIST]
    print(f"\n[1] 抓取 {len(all_symbols)} 個標的週線資料...")
    all_data = _parallel_fetch(all_symbols)

    market_data   = {sym: all_data[sym] for sym, _ in MARKET_SYMBOLS if sym in all_data}
    watchlist_data = {item["symbol"]: all_data[item["symbol"]]
                      for item in WATCHLIST if item["symbol"] in all_data}
    print(f"    大盤：{len(market_data)}/{len(MARKET_SYMBOLS)}　自選股：{len(watchlist_data)}/{len(WATCHLIST)}")

    # ── 2. 策略信號命中 ───────────────────────────────────────────
    print("\n[2] 查詢策略信號命中...")
    strategy_hits = get_strategy_hits()

    # ── 3. 生成報告 ───────────────────────────────────────────────
    print("\n[3] 生成週線回顧報告...")
    report = build_report(market_data, watchlist_data, strategy_hits, week_str)

    # ── 4. 發送 ───────────────────────────────────────────────────
    if dry_run:
        print("\n[dry-run] 報告預覽：")
        print("─" * 50)
        print(report)
        print("─" * 50)
    else:
        print("\n[4] 發送 Discord...")
        ok = send_discord(report)
        print(f"    {'✅ 發送成功' if ok else '❌ 發送失敗'}")

    print(f"\n✅ 週線回顧完成  {today.strftime('%H:%M:%S') if hasattr(today, 'strftime') else ''}")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="每週市場回顧報告")
    parser.add_argument("--dry-run", action="store_true", help="不發 Discord，只列印")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
