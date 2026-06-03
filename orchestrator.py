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

import os as _os
from shared.config import (
    ANTHROPIC_API_KEY, DISCORD_WATCHLIST, WATCHLIST
)
import watchlist_db as _watchlist_db
from shared.utils import (
    get_session_info, now_str, is_weekend,
    send_discord_message, send_embed, truncate, fmt_quote
)
from agents.market_data_agent import MarketDataAgent, fetch_yahoo, fetch_yahoo_extended, format_tech_for_prompt
from agents.news_agent         import NewsAgent
from agents.analyst_agent      import AnalystAgent, claude_call
from agents.publisher_agent    import PublisherAgent
from agents.crypto_agent       import CryptoAgent
from agents.sentiment_agent    import SentimentAgent
from agents.macro_agent        import MacroAgent
from agents.earnings_agent     import EarningsAgent


# ── AI 精選工具（00981A 每日成分股 → Claude 選 2~3 支）─────────────


def _load_candidate_stocks() -> list[dict]:
    """
    從 00981A 野村台灣趨勢動能 ETF 取得當日成分股作為 AI 精選候選池。
    此 ETF 為主動型，每日更新，能反映當天市場主要趨勢。
    API 失敗時使用靜態備用清單。
    """
    try:
        from strategy_agent.etf_universe import fetch_00981A_stocks
        stocks = fetch_00981A_stocks()
        if stocks:
            return [{"symbol": s["symbol"], "name": s["name"]} for s in stocks]
    except Exception as e:
        print(f"  ⚠️ 動態抓取 00981A 失敗：{e}，使用靜態備用清單")
    # 靜態備用（常見 00981A 成分）
    return [
        {"symbol": "2330.TW", "name": "台積電"},
        {"symbol": "2454.TW", "name": "聯發科"},
        {"symbol": "2308.TW", "name": "台達電"},
        {"symbol": "2382.TW", "name": "廣達"},
        {"symbol": "6669.TW", "name": "緯穎"},
        {"symbol": "4938.TW", "name": "和碩"},
        {"symbol": "2379.TW", "name": "瑞昱"},
        {"symbol": "3034.TW", "name": "聯詠"},
        {"symbol": "3711.TW", "name": "日月光投控"},
        {"symbol": "2317.TW", "name": "鴻海"},
    ]


def _ai_pick_from_candidates(candidates: list[dict], market_ctx: str) -> list[dict]:
    """
    讓 Claude 從 00981A 候選股中挑選今日最值得追蹤的 2~3 檔，
    回傳含 ai_reason 欄位的 list。
    """
    cand_list = "\n".join([f"股票:{s['symbol']}|名稱:{s['name']}" for s in candidates])
    prompt = f"""你是台股量化選股助理。今日 00981A 野村台灣趨勢動能 ETF 成分股行情如下：

{market_ctx if market_ctx else '（行情暫時無法取得）'}

候選股完整清單：
{cand_list}

請從中選出今日最值得追蹤的 2~3 支，每支格式如下（嚴格遵守）：
選股:股票代碼
理由1:技術面或籌碼面說明（一句）
理由2:題材或催化劑說明（一句）

繁體中文，每股最多 2 行理由，不得加其他文字。"""

    try:
        raw = claude_call(prompt, max_tokens=600)
    except Exception as e:
        print(f"  ❌ AI 精選 Claude 失敗：{e}")
        return [{**c, "ai_reason": "AI 精選"} for c in candidates[:2]]

    picks: list[dict] = []
    current_sym: str | None = None
    current_cand: dict | None = None
    current_reasons: list[str] = []

    def _flush() -> None:
        nonlocal current_sym, current_cand, current_reasons
        if current_sym and current_cand and current_reasons:
            picks.append({**current_cand, "ai_reason": "\n".join(current_reasons)})
        current_sym = current_cand = None
        current_reasons = []

    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("選股:"):
            _flush()
            sym = line[3:].strip()
            current_sym  = sym
            current_cand = next((c for c in candidates if c["symbol"] == sym), None)
            current_reasons = []
        elif line.startswith("理由") and current_cand is not None:
            current_reasons.append(line)
    _flush()

    if not picks:
        picks = [{**c, "ai_reason": "AI 精選"} for c in candidates[:2]]
    return picks[:3]


def _run_ai_pick(session_info: dict) -> None:
    """
    AI 精選：從 00981A 每日成分股中讓 Claude 選 2~3 支，
    發送 Embed 總覽 + 各股深度分析到 DISCORD_WATCHLIST。
    """
    print("\n🤖 AI 精選（00981A 每日成分股）...")
    ts             = now_str()
    weekend_banner = "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    candidates = _load_candidate_stocks()
    print(f"  📋 候選池：{len(candidates)} 支（00981A 今日成分股）")

    # 快速批量抓行情（僅用 fetch_yahoo，輕量）
    market_lines: list[str] = []
    for s in candidates:
        try:
            q = fetch_yahoo(s["symbol"], s["name"])
            if q.get("price") not in ("N/A", None, ""):
                market_lines.append(f"{s['name']}（{s['symbol']}）：{fmt_quote(q)}")
        except Exception:
            pass
        time.sleep(0.1)
    market_ctx = "\n".join(market_lines)

    # Claude 選股
    ai_picks = _ai_pick_from_candidates(candidates, market_ctx)
    print(f"  🤖 AI 精選：{', '.join([p['name'] for p in ai_picks])}")

    def _fmt_pick(p: dict) -> str:
        reasons = [l for l in p.get("ai_reason", "").split("\n") if l.strip()]
        header  = f"▸ **{p['name']}（{p['symbol']}）**"
        if not reasons:
            return header
        if len(reasons) == 1:
            return f"{header} — {reasons[0]}"
        return f"{header}\n" + "\n".join(f"　{l}" for l in reasons)

    picks_desc = "\n\n".join([_fmt_pick(p) for p in ai_picks])

    # 發送總覽 Embed
    send_embed(DISCORD_WATCHLIST, {
        "title": f"🤖 AI 精選 {session_info['emoji']} {weekend_banner}| {ts}",
        "description": (
            f"候選池：**00981A 野村台灣趨勢動能 ETF** 今日成分股（{len(candidates)} 支）\n\n"
            f"**今日 AI 精選理由：**\n{picks_desc}\n\n"
            "由 Claude AI 生成，不構成投資建議。"
        ),
        "color":  0x9B59B6,   # 紫色（區別自選股橙色）
        "footer": {"text": "來源：TWSE 00981A + Yahoo Finance + Claude AI"},
    })
    time.sleep(1.2)

    # 逐支深度分析
    # 每支股票分析最多等 90 秒，超時直接跳過（避免 Claude 慢拖延整體）
    _STOCK_TIMEOUT = 150

    for pick in ai_picks:
        symbol = pick["symbol"]
        name   = pick["name"]
        print(f"  分析 {name}（{symbol}）...", flush=True)
        try:
            def _analyse_one(p=pick, s=symbol, n=name):
                quote    = fetch_yahoo_extended(s, n)
                news_ctx = _fetch_stock_news(s, n)
                fund     = _fetch_fundamentals(s)
                chip     = _fetch_chip(s)
                analysis = _analyze_single_stock(s, n, quote, news_ctx, fund, chip)
                return quote, analysis

            with ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(_analyse_one)
                try:
                    quote, analysis = _fut.result(timeout=_STOCK_TIMEOUT)
                except Exception as te:
                    print(f"  ⏱️ {name} 分析超時或失敗（{te}），發送提示", flush=True)
                    send_discord_message(
                        DISCORD_WATCHLIST,
                        f"## 🤖 **{name}（{symbol}）**\n"
                        f"⏳ AI 分析超時（>{_STOCK_TIMEOUT}s），本次略過。\n"
                        f"可使用 `/查股 {symbol}` 手動查詢。"
                    )
                    continue

            reason_lines = [l for l in pick.get("ai_reason", "").split("\n") if l.strip()]
            ai_tag = (
                "\n**🤖 AI 精選理由：**\n" + "\n".join(f"> {l}" for l in reason_lines)
            ) if reason_lines else ""
            stale_tag = " 📅上一交易日" if quote.get("stale") else ""

            send_discord_message(
                DISCORD_WATCHLIST,
                f"## {quote.get('emoji', '🤖')} **{name}（{symbol}）** — "
                f"{quote.get('price', 'N/A')} ({quote.get('pct', 'N/A')}){stale_tag}{ai_tag}\n\n{analysis}"
            )
        except Exception as e:
            print(f"  ❌ {name}（{symbol}）AI 精選分析失敗：{e}", flush=True)
        time.sleep(1.5)

    print("  ✅ AI 精選完成", flush=True)


# ── 自選股工具（僅 22:00 盤後使用）────────────────────────────────

# TWSE 基本面快取（每次 bot 啟動後 4 小時有效，避免重複打 API）
_TWSE_FUND_CACHE: dict = {}
_TWSE_FUND_TS: float = 0.0
_TWSE_FUND_TTL: float = 4 * 3600


def _fetch_twse_fund_map() -> dict:
    """批次取得 TWSE 全市場 PE/PB/殖利率，含快取"""
    global _TWSE_FUND_CACHE, _TWSE_FUND_TS
    import time as _t
    now = _t.time()
    if _TWSE_FUND_CACHE and now - _TWSE_FUND_TS < _TWSE_FUND_TTL:
        return _TWSE_FUND_CACHE
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if r.status_code != 200:
            return _TWSE_FUND_CACHE
        data = {}
        for item in r.json():
            code = str(item.get("Code", "")).strip()
            if not code:
                continue
            try:
                data[code] = {
                    "pe":    float(item["PEratio"])       if item.get("PEratio")       else None,
                    "pb":    float(item["PBratio"])       if item.get("PBratio")       else None,
                    "yield": float(item["DividendYield"]) if item.get("DividendYield") else None,
                }
            except (ValueError, KeyError):
                pass
        _TWSE_FUND_CACHE = data
        _TWSE_FUND_TS    = now
        return data
    except Exception as e:
        print(f"  ⚠️ TWSE 基本面 API 失敗：{e}", flush=True)
        return _TWSE_FUND_CACHE


def _fetch_fundamentals(symbol: str) -> dict:
    """取得台股基本面（PE/PB/殖利率）+ Yahoo 52週高低 + EPS/ROE"""
    result: dict = {}
    is_tw = symbol.upper().endswith(".TW")
    tw_code = symbol.upper().removesuffix(".TW") if is_tw else ""

    # TWSE OpenAPI（PE/PB/殖利率）
    if tw_code:
        fund_map = _fetch_twse_fund_map()
        f = fund_map.get(tw_code, {})
        result.update({k: v for k, v in f.items() if v is not None})

    # Yahoo Finance summary（EPS / ROE / 52週高低）
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v11/finance/quoteSummary/{symbol}",
            params={"modules": "defaultKeyStatistics,financialData"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
        if r.status_code == 200:
            s = r.json().get("quoteSummary", {}).get("result", [{}])[0]
            ks = s.get("defaultKeyStatistics", {})
            fd = s.get("financialData", {})
            if ks.get("trailingEps",  {}).get("raw"): result["eps"] = round(ks["trailingEps"]["raw"], 2)
            if fd.get("returnOnEquity", {}).get("raw"): result["roe"] = round(fd["returnOnEquity"]["raw"] * 100, 1)
            if fd.get("revenueGrowth", {}).get("raw"):  result["rev_yoy"] = round(fd["revenueGrowth"]["raw"] * 100, 1)
    except Exception:
        pass

    return result


def _fetch_chip(symbol: str) -> dict:
    """取得台股三大法人當日買賣超（TWSE OpenAPI）"""
    if not symbol.upper().endswith(".TW"):
        return {}
    tw_code = symbol.upper().removesuffix(".TW")
    try:
        import datetime as _dt
        today = _dt.date.today().strftime("%Y%m%d")
        r = requests.get(
            "https://www.twse.com.tw/rwd/zh/fund/T86",
            params={"response": "json", "date": today, "selectType": "ALLBUT0999"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        rows = data.get("data", [])
        for row in rows:
            if str(row[0]).strip() == tw_code:
                # row: [代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨, 自營買, 自營賣, 自營淨, 三大合計]
                def _parse(v):
                    try: return int(str(v).replace(",", ""))
                    except: return None
                return {
                    "foreign_net": _parse(row[4]),
                    "trust_net":   _parse(row[7]),
                    "dealer_net":  _parse(row[10]),
                    "total_net":   _parse(row[11]),
                }
    except Exception as e:
        print(f"  ⚠️ 籌碼面 API 失敗 {symbol}：{e}", flush=True)
    return {}


def _fmt_fund(fund: dict) -> str:
    """格式化基本面為單行文字"""
    parts = []
    if fund.get("pe")      is not None: parts.append(f"本益比 {fund['pe']:.1f}x")
    if fund.get("pb")      is not None: parts.append(f"PBR {fund['pb']:.2f}x")
    if fund.get("yield")   is not None: parts.append(f"殖利率 {fund['yield']:.2f}%")
    if fund.get("eps")     is not None: parts.append(f"EPS {fund['eps']}")
    if fund.get("roe")     is not None: parts.append(f"ROE {fund['roe']}%")
    if fund.get("rev_yoy") is not None: parts.append(f"營收YoY {fund['rev_yoy']:+.1f}%")
    return "  ".join(parts) if parts else "（無資料）"


def _fmt_chip(chip: dict) -> str:
    """格式化籌碼面為單行文字"""
    if not chip:
        return "（無資料）"
    parts = []
    def _k(v): return f"{v/1000:.1f}千" if v is not None and abs(v) >= 1000 else (str(v) if v is not None else "?")
    parts.append(f"外資 {_k(chip.get('foreign_net'))}")
    parts.append(f"投信 {_k(chip.get('trust_net'))}")
    parts.append(f"自營 {_k(chip.get('dealer_net'))}")
    total = chip.get("total_net")
    if total is not None:
        direction = "買超" if total > 0 else "賣超"
        parts.append(f"合計 {_k(total)}（{direction}）")
    return "  ".join(parts)


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

def _analyze_single_stock(
    symbol: str, name: str, quote: dict,
    news_ctx: str = "", fund: dict = None, chip: dict = None
) -> str:
    """Claude 分析單一股票（技術面 + 籌碼面 + 基本面 + 消息面）"""
    stale_note = "⚠️ 注意：今日為週末，以下為上次交易日收盤數據。\n" if quote.get("stale") else ""

    # 技術指標（fetch_yahoo_extended 已算好，掛在 quote["tech"]）
    tech = quote.get("tech", {})
    tech_text = format_tech_for_prompt(tech)

    # 基本面 / 籌碼面格式化
    fund_text = _fmt_fund(fund) if fund else "（無資料）"
    chip_text = _fmt_chip(chip) if chip else "（無資料）"

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析與預測。

{stale_note}【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote.get('price', 'N/A')}　漲跌：{quote.get('change', 'N/A')}（{quote.get('pct', 'N/A')}）

【技術指標】
{tech_text}

【籌碼面（三大法人今日買賣超，千股）】
{chip_text}

【基本面】
{fund_text}

【消息面（近期新聞）】
{news_ctx if news_ctx else '（暫無相關新聞）'}

請以繁體中文撰寫分析報告（禁 Markdown 表格，全程 bullet（•），**800字以內，務必完整輸出所有段落**）：

1. **近期走勢**（均線 + 動能，2句）
2. **技術訊號**
   • 均線結構（MA5/20/60）→ 偏多/偏空
   • MACD / RSI / KDJ → 強弱訊號
   • 量比 → 量能配合度
3. **籌碼與基本面**
   • 三大法人方向 → 意涵（買超/賣超 → 機構態度）
   • 基本面評估（本益比合理性 / ROE / 營收動能）
4. **消息面影響**（若有重要新聞則說明影響方向，否則一句帶過）
5. **支撐與壓力**（關鍵價位，帶具體數字）
6. **操作建議**
   • 多方：進場條件 → 目標 → 停損
   • 空方：觸發條件 → 目標 → 停損
   • 風險提示（1句）

> ⚠️ AI 生成分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1200)

def _run_watchlist_report(session_info: dict) -> None:
    """自選股日報（每日 22:00 盤後執行）"""
    print("\n📈 執行自選股分析...")
    ts = now_str()
    weekend_banner = "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    # ── 優先讀 Discord 自選股 DB（所有用戶加過的股票，去重合併）──
    guild_id = _os.environ.get("WATCHLIST_GUILD_ID", "")
    _watchlist_db.init_db()
    db_stocks = _watchlist_db.get_all_symbols(guild_id or None)

    if not db_stocks:
        print("  ℹ️ 自選股 DB 無資料，跳過（請用 /新增自選股 加入股票）", flush=True)
        # 仍執行 AI 精選與加密快照（不依賴自選股）
        time.sleep(2)
        _run_ai_pick(session_info)
        time.sleep(2)
        try:
            CryptoAgent().run()
        except Exception as e:
            print(f"  ⚠️ CryptoAgent 失敗（不影響其他功能）：{e}")
        return

    stocks   = db_stocks
    src_note = f"來自 Discord 自選股（{len(stocks)} 支）"
    print(f"  📋 自選股來源：{src_note}", flush=True)

    send_embed(DISCORD_WATCHLIST, {
        "title": f"📊 自選股日報 {session_info['emoji']} {session_info['label']} {weekend_banner}| {ts}",
        "description": (
            f"**時段：** {session_info['period']}\n"
            f"**追蹤：** {src_note}\n"
            "以下為自選股最新分析，由 Claude AI 生成，不構成投資建議。"
        ),
        "color": 0xF39C12,
        "footer": {"text": "資料來源：Yahoo Finance + Claude AI"},
    })
    time.sleep(1.2)

    _STOCK_TIMEOUT = 150   # 每支最多 150 秒，超時發提示

    for stock in stocks:
        symbol = stock["symbol"]
        name   = stock["name"]
        print(f"  分析 {name}（{symbol}）...", flush=True)
        try:
            def _analyse_watchlist(s=symbol, n=name):
                q    = fetch_yahoo_extended(s, n)
                ctx  = _fetch_stock_news(s, n)
                fund = _fetch_fundamentals(s)
                chip = _fetch_chip(s)
                ana  = _analyze_single_stock(s, n, q, ctx, fund, chip)
                return q, ana

            with ThreadPoolExecutor(max_workers=1) as _ex:
                _fut = _ex.submit(_analyse_watchlist)
                try:
                    quote, analysis = _fut.result(timeout=_STOCK_TIMEOUT)
                except Exception as te:
                    print(f"  ⏱️ {name} 超時或失敗（{te}），發送提示", flush=True)
                    send_discord_message(
                        DISCORD_WATCHLIST,
                        f"## 📊 **{name}（{symbol}）**\n"
                        f"⏳ AI 分析超時（>{_STOCK_TIMEOUT}s），本次略過。\n"
                        f"可使用 `/查股 {symbol}` 手動查詢。"
                    )
                    continue

            stale_tag = " 📅上一交易日" if quote.get("stale") else ""
            send_discord_message(
                DISCORD_WATCHLIST,
                f"## {quote.get('emoji','📊')} **{name}（{symbol}）** — "
                f"{quote.get('price','N/A')} ({quote.get('pct','N/A')}){stale_tag}\n\n{analysis}"
            )
        except Exception as e:
            print(f"  ❌ {name}（{symbol}）分析失敗，跳過：{e}", flush=True)
        time.sleep(1.5)

    # ── AI 精選（00981A 每日成分股，附於自選股分析之後）───────────
    time.sleep(2)
    _run_ai_pick(session_info)

    # ── 加密貨幣市場快照（CoinGecko + F&G + Binance 資金費率）──────
    time.sleep(2)
    try:
        CryptoAgent().run()
    except Exception as e:
        print(f"  ⚠️ CryptoAgent 失敗（不影響其他功能）：{e}")

    print("  ✅ 自選股分析完成")


def run_watchlist_now() -> None:
    """
    On-demand 執行自選股分析 + AI 精選（不限時段）。
    供 Discord /自選股 指令在 Railway 直接呼叫。
    """
    session_info = {
        "label":  "盤後晚報",
        "emoji":  "🌙",
        "period": "15:00 ~ 22:00",
    }
    _run_watchlist_report(session_info)


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

    # ── Phase 3.5：市場情緒 + 總體環境（每次報告都發，各自保護）────
    try:
        SentimentAgent().run(market_data)
    except Exception as e:
        print(f"  ⚠️ SentimentAgent 失敗（不影響其他功能）：{e}")

    try:
        MacroAgent().run()
    except Exception as e:
        print(f"  ⚠️ MacroAgent 失敗（不影響其他功能）：{e}")

    # 財報行事曆：僅 08:00 早報發送（一天一次即可）
    if session_info["label"] == "盤前早報":
        try:
            EarningsAgent().run()
        except Exception as e:
            print(f"  ⚠️ EarningsAgent 失敗（不影響其他功能）：{e}")

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
