"""
orchestrator.py — 總指揮
協調所有 Agent，並行收集資料、並行分析、依序發送

執行方式：
  python orchestrator.py              ← 立即執行一次
  python orchestrator.py --schedule   ← 啟動排程（08:00 / 14:00 / 22:00）
"""

import io
import sys
import time
import datetime
import schedule
import requests
import feedparser

# 強制 UTF-8（解決 Windows cp950 問題）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from concurrent.futures import ThreadPoolExecutor

from shared.config import (
    ANTHROPIC_API_KEY, DISCORD_WATCHLIST, WATCHLIST
)
from shared.utils import (
    get_session_info, now_str, is_weekend,
    send_discord_message, send_embed, truncate, fmt_quote
)
from agents.market_data_agent import MarketDataAgent, fetch_yahoo
from agents.news_agent         import NewsAgent
from agents.analyst_agent      import AnalystAgent, claude_call
from agents.publisher_agent    import PublisherAgent


# ── 自選股工具（僅 22:00 盤後使用）────────────────────────────────

def _fetch_stock_news(symbol: str, name: str) -> str:
    """抓取單一股票相關新聞標題"""
    query = f"{name} 台股 股票" if ".TW" in symbol else f"{name} {symbol} stock"
    try:
        url = (
            f"https://news.google.com/rss/search?"
            f"q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        titles = [e.title for e in feed.entries[:5] if hasattr(e, "title")]
        return "\n".join(titles) if titles else ""
    except Exception:
        return ""

def _analyze_single_stock(symbol: str, name: str, quote: dict, news_ctx: str = "") -> str:
    """Claude 分析單一股票"""
    stale_note = "⚠️ 注意：今日為週末，以下為上次交易日收盤數據。\n" if quote.get("stale") else ""
    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析與預測。

{stale_note}【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote.get('price', 'N/A')}
- 漲跌幅：{quote.get('pct', 'N/A')}
- 漲跌點：{quote.get('change', 'N/A')}

【相關新聞背景】
{news_ctx if news_ctx else '（暫無相關新聞）'}

請以繁體中文撰寫分析報告，格式如下：

1. **近期走勢分析**（2-3句）
2. **技術面觀察**（均線、支撐壓力位、RSI/MACD）
3. **基本面亮點**（2-3個重點，結合近期新聞）
4. **短期預測**（未來 1-2 週走向，給出目標價區間）
5. **操作建議**
   - 多方策略：...
   - 空方策略：...
   - 風險提示：...

請保持專業、客觀，並明確標示這是 AI 分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1200)

def _run_watchlist_report(session_info: dict) -> None:
    """自選股日報（每日 22:00 盤後執行）"""
    print("\n📈 執行自選股分析...")
    ts = now_str()
    weekend_banner = "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    send_embed(DISCORD_WATCHLIST, {
        "title": f"📊 自選股日報 {session_info['emoji']} {session_info['label']} {weekend_banner}| {ts}",
        "description": (
            f"**時段：** {session_info['period']}\n"
            "以下為自選股最新分析，由 Claude AI 生成，不構成投資建議。"
        ),
        "color": 0xF39C12,
        "footer": {"text": "資料來源：Yahoo Finance + Claude AI"},
    })
    time.sleep(1.2)

    for stock in WATCHLIST:
        symbol = stock["symbol"]
        name   = stock["name"]
        print(f"  分析 {name}（{symbol}）...")

        quote    = fetch_yahoo(symbol, name)
        news_ctx = _fetch_stock_news(symbol, name)
        time.sleep(0.5)

        analysis = _analyze_single_stock(symbol, name, quote, news_ctx)
        time.sleep(1)

        stale_tag = " ⚠️未更新" if quote.get("stale") else ""
        send_discord_message(
            DISCORD_WATCHLIST,
            f"## {quote.get('emoji','📊')} **{name}（{symbol}）** — "
            f"{quote.get('price','N/A')} ({quote.get('pct','N/A')}){stale_tag}\n\n{analysis}"
        )
        time.sleep(1.5)

    print("  ✅ 自選股分析完成")


# ── 主執行函式 ─────────────────────────────────────────────────────

def run_report() -> None:
    """
    執行一次完整日報：
    Phase 1（並行）→ 大盤數據 + 新聞收集
    Phase 2（並行）→ Claude 分析
    Phase 3（依序）→ Discord 發送
    Phase 4（選擇）→ 22:00 自選股報告
    """
    print("=" * 65)
    session_info = get_session_info()
    weekend_note = "（週末版）" if is_weekend() else ""
    print(f"🚀 股市日報啟動 — {now_str()} {weekend_note}")
    print(f"📅 本次時段：{session_info['emoji']} {session_info['label']} ({session_info['period']})")
    print("=" * 65)

    # ── Phase 1：並行收集（大盤數據 + 新聞同時跑）──────────────────
    print("\n⚡ Phase 1：並行收集資料...")
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_market = executor.submit(MarketDataAgent().run)
        future_news   = executor.submit(NewsAgent().run, session_info)
        market_data   = future_market.result()
        news_data     = future_news.result()
    print(f"✅ Phase 1 完成，耗時 {time.time() - t1:.1f} 秒\n")

    # ── Phase 2：並行分析（台股 / 美股 / 全球同時送 Claude）────────
    print("⚡ Phase 2：並行 Claude 分析...")
    t2 = time.time()
    analysis = AnalystAgent().run(market_data, news_data, session_info)
    print(f"✅ Phase 2 完成，耗時 {time.time() - t2:.1f} 秒\n")

    # ── Phase 3：依序發送 Discord ────────────────────────────────────
    print("⚡ Phase 3：發送 Discord...")
    t3 = time.time()
    PublisherAgent().run(market_data, news_data, analysis, session_info)
    print(f"✅ Phase 3 完成，耗時 {time.time() - t3:.1f} 秒\n")

    # ── Phase 4：自選股（僅 22:00 盤後）─────────────────────────────
    if session_info["label"] == "盤後晚報":
        time.sleep(2)
        _run_watchlist_report(session_info)

    total = time.time() - t1
    print("\n" + "=" * 65)
    print(f"✅ 日報完成！總耗時 {total:.1f} 秒 — {now_str()}")
    print("=" * 65)


# ── 排程 ──────────────────────────────────────────────────────────

def run_schedule() -> None:
    """啟動排程模式（每日 08:00 / 14:00 / 22:00 台灣時間，含週六日）"""
    print("=" * 65)
    print("⏰ 排程模式啟動")
    print("   每日 08:00 / 14:00 / 22:00（台灣時間）自動執行")
    print("   週六日照常執行，指標數據標註「未更新」")
    print("   按 Ctrl+C 停止")
    print("=" * 65)

    schedule.every().day.at("08:00").do(run_report)
    schedule.every().day.at("14:00").do(run_report)
    schedule.every().day.at("22:00").do(run_report)

    print(f"⏭️  下次執行：{schedule.next_run()}")

    while True:
        schedule.run_pending()
        time.sleep(30)


# ── 入口 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("⚠️  警告：未設定 ANTHROPIC_API_KEY 環境變數！")
        print("   AI 分析功能將無法使用。\n")

    if "--schedule" in sys.argv:
        run_schedule()
    else:
        print("💡 提示：加上 --schedule 參數可啟動自動排程模式")
        run_report()
