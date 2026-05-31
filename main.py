import os
import re
import sys
import time
import threading
import asyncio
import datetime
import requests
import feedparser
import discord
from discord import app_commands
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Health check server for Railway PORT binding ──────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _start_health():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()

threading.Thread(target=_start_health, daemon=True).start()
print("=== Discord Bot starting ===", flush=True)

# ── local imports ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technical_indicators import (get_full_indicators, format_indicators_for_prompt,
                                  get_data_quality_report, classify_signals, format_signal_tiers,
                                  calc_macd, calc_rsi, calc_kdj, calc_bias, calc_volume)
from warrant import search_warrants, analyze_warrant, lookup_stock, prewarm_cache
from warrant import _fetch_mis, _ratio
import warrant_db

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")
# 查詢指令限制頻道（只有此頻道可使用 /查股 /查權證 /分析權證）
# 設定方法：Discord 開啟開發者模式 → 右鍵頻道 → 複製頻道 ID → 填入 QUERY_CHANNEL_ID
# 留空 = 所有頻道都開放查詢
QUERY_CHANNEL_ID  = os.environ.get("QUERY_CHANNEL_ID", "")
# Claude 模型名稱（可透過環境變數覆寫，避免 Anthropic 改版時需動程式碼）
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

import pytz
TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

# ── Anthropic SDK（取代 requests.post 直接呼叫）────────────────
from shared.claude_client import simple_call as _sdk_call, stream_call as _async_stream
import watchlist_db

# ── Strategy Agent（延遲載入，避免啟動時 DB 不存在）────────────
def _run_strategy_agent():
    """在背景執行策略 Agent（盤後 17:00 觸發）"""
    try:
        from strategy_agent.runner import run as _strategy_run, init_db as _strategy_init
        _strategy_init()
        _strategy_run(dry_run=False)
    except Exception as e:
        print(f"❌ 策略 Agent 執行失敗：{e}", flush=True)

def _get_strategy_today() -> str:
    """取得今日策略信號摘要（給 /明牌 指令用）"""
    try:
        from strategy_agent.runner import get_today_summary
        from strategy_agent.signal_db import init_db
        init_db()
        return get_today_summary()
    except Exception as e:
        return f"❌ 無法取得策略信號：{e}"

def _get_strategy_winrate() -> str:
    """取得策略勝率統計（給 /明牌 指令用）"""
    try:
        from strategy_agent.runner import get_winrate_summary
        from strategy_agent.signal_db import init_db
        init_db()
        return get_winrate_summary()
    except Exception as e:
        return f"❌ 無法取得勝率統計：{e}"

def _claude_call(prompt: str, max_tokens: int = 1600) -> str:
    """
    Discord bot 同步 Claude 呼叫（不串流版）。
    用於期貨試算、查權證等還不需要串流的指令。
    """
    return _sdk_call(prompt, max_tokens=max_tokens, model=CLAUDE_MODEL)


async def _stream_to_discord(
    prompt: str,
    msg: discord.Message,
    prefix: str = "",
    max_tokens: int = 2400,
) -> str:
    """
    串流 Claude 回應並即時更新 Discord 訊息。
    • 每 1.5 秒 edit 一次（避免 Discord rate limit）
    • 顯示進度游標 ▌
    • 回傳完整輸出文字
    """
    import time as _time
    accumulated = ""
    last_edit   = _time.monotonic()

    try:
        async for chunk in _async_stream(prompt, max_tokens=max_tokens, model=CLAUDE_MODEL):
            accumulated += chunk
            now = _time.monotonic()
            if now - last_edit >= 1.5:
                display = (prefix + accumulated)[:1950] + " ▌"
                try:
                    await msg.edit(content=display)
                except Exception:
                    pass
                last_edit = now
    except Exception as e:
        print(f"❌ _stream_to_discord 例外：{e}", flush=True)
        if not accumulated:
            accumulated = _claude_call(prompt, max_tokens)   # fallback

    return prefix + accumulated

def _fetch_quote(symbol: str) -> dict | None:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return None
        data   = r.json()
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        volumes = result["indicators"]["quote"][0].get("volume", [])
        meta   = result.get("meta", {})
        if len(closes) < 2:
            return None
        prev, curr = closes[-2], closes[-1]
        chg  = curr - prev
        pct  = chg / prev * 100
        vols = [v for v in volumes if v is not None]
        return {
            "name":     meta.get("shortName", symbol),
            "price":    f"{curr:,.2f}",
            "change":   f"{chg:+.2f}",
            "pct":      f"{pct:+.2f}%",
            "emoji":    "🔴" if pct < 0 else "🟢",
            "currency": meta.get("currency", ""),
            "volume":   f"{vols[-1]:,.0f}" if vols else "N/A",
            "closes":   closes,
        }
    except Exception as e:
        print(f"Yahoo 錯誤：{e}", flush=True)
        return None

def _fetch_news(symbol: str, name: str) -> str:
    titles, seen = [], set()
    def _add(url):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:6]:
                t = getattr(e, "title", "").strip()
                key = re.sub(r"\s+", "", t.lower())[:25]
                if t and key not in seen:
                    seen.add(key)
                    titles.append(t)
        except Exception:
            pass
    if ".TW" in symbol:
        stock_no = symbol.replace(".TW", "")
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' 台股')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        time.sleep(0.2)
        _add("https://news.cnyes.com/rss/cat/tw_stock_news")
        time.sleep(0.2)
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(stock_no)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' stock')}&hl=en-US&gl=US&ceid=US:en")
        time.sleep(0.2)
        _add(f"https://finance.yahoo.com/rss/headline?s={symbol}")
    return "\n".join(titles[:8]) if titles else "（暫無相關新聞）"

def _fetch_tech_fast(symbol: str) -> str:
    """
    從 Yahoo Finance v8 API 抓取 3 個月 OHLCV，
    用純 Python 計算技術指標（不呼叫 yfinance，速度快 ≈1-2s）。
    回傳格式化純文字，供 Claude 分析用。
    """
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=3mo",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        if r.status_code != 200:
            return "（技術指標暫時無法取得）"

        result = r.json()["chart"]["result"][0]
        qdata  = result["indicators"]["quote"][0]

        # 過濾出 close/high/low 都有值的 bar
        valid = [
            (c, h, l, v)
            for c, h, l, v in zip(
                qdata.get("close",  []), qdata.get("high",   []),
                qdata.get("low",    []), qdata.get("volume", [])
            )
            if c is not None and h is not None and l is not None
        ]
        if len(valid) < 20:
            return "（歷史資料不足，無法計算技術指標）"

        closes  = [x[0] for x in valid]
        highs   = [x[1] for x in valid]
        lows    = [x[2] for x in valid]
        volumes = [x[3] if x[3] is not None else 0 for x in valid]
        curr    = closes[-1]

        # 均線
        def _ma(n: int):
            return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None

        ma5, ma10, ma20 = _ma(5), _ma(10), _ma(20)
        ma60 = _ma(60) if len(closes) >= 30 else None

        lines = []

        # ── 均線
        ma_parts = []
        for p, v in [(5, ma5), (10, ma10), (20, ma20), (60, ma60)]:
            if v:
                is_lg = curr > 1000
                vf = f"{v:,.0f}" if is_lg else f"{v:,.2f}"
                ma_parts.append(f"MA{p}={vf}（現價{'上方' if curr >= v else '下方'}）")
        if ma_parts:
            lines.append("均線：" + " | ".join(ma_parts))

        # ── MACD
        m = calc_macd(closes)
        if m.get("macd") is not None:
            cross = ""
            if m.get("golden_cross"):
                cross = " ★黃金交叉"
            elif m.get("dead_cross"):
                cross = " ★死亡交叉"
            lines.append(
                f"MACD={m['macd']} Signal={m['signal']} 柱={m['hist']} "
                f"{m['trend']}{cross}"
            )

        # ── RSI
        rd = calc_rsi(closes)
        if rd.get("rsi") is not None:
            lines.append(f"RSI(14)={rd['rsi']} — {rd['signal']}")

        # ── KDJ
        kd = calc_kdj(highs, lows, closes)
        if kd.get("k") is not None:
            lines.append(f"KDJ：K={kd['k']} D={kd['d']} J={kd['j']} — {kd['signal']}")

        # ── 乖離率
        b = calc_bias(closes)
        bp = [f"{p}日={b[f'bias{p}']:+.2f}%" for p in [5, 10, 20] if b.get(f"bias{p}") is not None]
        if bp:
            lines.append("乖離率：" + " | ".join(bp))

        # ── 量比
        vd = calc_volume(volumes)
        if vd.get("vol_ratio") is not None:
            lines.append(f"量比={vd['vol_ratio']}x — {vd['vol_trend']}")

        # ── 近20日支撐壓力（依實際高低點）
        is_large = curr > 1000
        fmt = lambda v: f"{v:,.0f}" if is_large else f"{v:,.2f}"
        r_hi = sorted(highs[-20:], reverse=True)[:3]
        r_lo = sorted(lows[-20:])[:3]
        lines.append("近20日壓力參考：" + " / ".join([fmt(h) for h in r_hi]))
        lines.append("近20日支撐參考：" + " / ".join([fmt(l) for l in r_lo]))

        return "\n".join(lines)

    except Exception as e:
        print(f"⚠️ _fetch_tech_fast 錯誤 {symbol}: {e}", flush=True)
        return "（技術指標暫時無法取得）"


def _prepare_analysis(symbol: str, name: str) -> dict:
    """
    抓取股票資料並組好 prompt，供串流版 / 同步版共用。
    回傳：
      {"error": str}          → 找不到股票
      {"error": None, "header": str, "prompt": str}  → 正常
    """
    quote = _fetch_quote(symbol)
    if not quote:
        return {
            "error": (
                f"❌ 找不到股票代碼 `{symbol}`\n"
                f"格式範例：台股 `2330.TW`、美股 `NVDA`、指數 `^TWII`"
            )
        }
    display_name = name or quote.get("name", symbol)
    news     = _fetch_news(symbol, display_name)
    tech_txt = _fetch_tech_fast(symbol)

    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        is_lg = float(closes[-1]) > 1000
        fmt_p = (lambda v: f"{v:,.0f}") if is_lg else (lambda v: f"{v:,.2f}")
        price_history = "近5日收盤：" + " → ".join([fmt_p(c) for c in closes[-5:]])

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析。

【注意】
- 直接開始寫分析內容，禁空話，禁 Markdown 表格，全程 bullet（•）
- 台灣慣例：🔴=漲，🟢=跌
- 每個論點格式：「數值/事實 → 機制 → 影響方向」
- 總字數 1100 字以內

【標的資訊】
代碼：{symbol}　名稱：{display_name}
現價：{quote['price']} {quote.get('currency', '')}　漲跌：{quote['change']}（{quote['pct']}）
{price_history}

【技術指標（近3個月，Yahoo Finance + 純 Python 計算）】
{tech_txt}

【近期相關新聞】
{news}

━━━ 以下四節全部完整輸出 ━━━

**A. 歸因分析（必做）**
• 識別 1-3 個近期走勢主要驅動力（技術突破/籌碼主導/消息催化/總體環境）
• 結合技術指標說明目前趨勢結構：均線位置（多/空排列）＋MACD/RSI 動能
• 這是短暫脈衝（1-5日）還是持續結構趨勢？判斷依據？

**B. 技術訊號整合（必做）**
• 均線多空排列 → 短/中/長期偏向
• MACD 動能（柱狀方向＋是否交叉）+ RSI/KDJ 超買超賣狀態
• 量比配合：量增/縮 → 說明趨勢健康度
• **關鍵壓力位**（帶具體數字，參考近20日高點）
• **關鍵支撐位**（帶具體數字，參考近20日低點及均線）

**C. 多空決策（必做）**
• 多方論點（技術+消息面）→ 目標價位（數字）
• 空方論點（風險因素）→ 止損建議（數字）
• 當前偏向：多/空/觀望 + 最關鍵決定因素一句

**D. 操作建議（必做）**
• 進場條件（突破什麼位置才進？）
• 目標價位 → 停損位（具體數字）
• 信心水準（高/中/低）+ 最大不確定因子（1句）

> ⚠️ AI 生成分析，不構成投資建議。"""

    header = (
        f"## {quote['emoji']} **{display_name}（{symbol}）** | {now_str()}\n"
        f"**現價：{quote['price']} {quote.get('currency','')}**　"
        f"漲跌：{quote['change']}（{quote['pct']}）\n"
        f"{'─' * 28}\n\n"
    )
    return {"error": None, "header": header, "prompt": prompt}


def _do_analysis(symbol: str, name: str) -> str:
    """同步版查股分析（期貨/自選股等指令的 fallback 仍可使用）"""
    data = _prepare_analysis(symbol, name)
    if data["error"]:
        return data["error"]
    return data["header"] + _claude_call(data["prompt"], max_tokens=2400)


# ── 市場新聞抓取（/新聞 指令用）──────────────────────────────────

_MARKET_NEWS_FEEDS: dict[str, list[str]] = {
    "台股": [
        "https://news.cnyes.com/rss/cat/tw_stock",
        "https://news.google.com/rss/search?q=台股+股市+今日&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=台灣+財經+科技股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    ],
    "美股": [
        "https://finance.yahoo.com/rss/topstories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "https://news.google.com/rss/search?q=US+stock+market+Wall+Street+today&hl=en-US&gl=US&ceid=US:en",
    ],
    "國際": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://news.google.com/rss/search?q=global+economy+Fed+inflation+oil&hl=en-US&gl=US&ceid=US:en",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],
}

def _fetch_market_news(market_type: str) -> list[str]:
    """抓取市場新聞標題（去重，最多 10 則）"""
    urls = _MARKET_NEWS_FEEDS.get(market_type, _MARKET_NEWS_FEEDS["台股"])
    titles, seen = [], set()
    for url in urls:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:6]:
                t = getattr(e, "title", "").strip()
                key = re.sub(r"\s+", "", t.lower())[:30]
                if t and key not in seen:
                    seen.add(key)
                    titles.append(t)
            time.sleep(0.3)
        except Exception:
            pass
    return titles[:10]

def _trigger_workflow(workflow_file: str, inputs: dict) -> tuple[bool, str]:
    if not GITHUB_PAT:
        return False, "未設定 GITHUB_PAT"
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches",
            headers={
                "Authorization": f"token {GITHUB_PAT}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"ref": "main", "inputs": inputs},
            timeout=10,
        )
        if r.status_code == 204:
            return True, ""
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:80]

# ── 定時觸發日報（Railway 永遠在線，取代不可靠的 GitHub cron）────

# ── 個股期貨工具函式 ──────────────────────────────────────────────

def _stock_code_to_yf(code: str) -> str:
    """台股代碼 → Yahoo Finance symbol（2330 → 2330.TW）"""
    c = code.strip().upper()
    if c.endswith(".TW") or c.endswith(".TWO"):
        return c
    if c.isdigit():
        return f"{c}.TW"
    return c   # 已是其他格式（NVDA 等）就直接回傳

def _estimate_lot_size(price: float) -> int:
    """
    依股價估算 TAIFEX 個股期貨「標準口」每口股數（lot size）。
    實際規格請至 TAIFEX 官網確認；此為近似值。
    TAIFEX 調整依據：讓每口合約市值約落在 50-150 萬元區間。
    """
    if price >= 1000:   return   500   # 台積電等超高價股
    if price >= 500:    return  1000
    if price >= 100:    return  2000   # 最常見：聯發科、廣達…
    if price >= 50:     return  4000
    if price >= 10:     return 10000
    return 20000

# 合約規模倍率表（小型 = ½ 標準，微型 = ⅕ 標準）
_SIZE_TYPE_INFO = {
    "標準": {"ratio": 1.0,  "label": "標準",         "tag": ""},
    "小型": {"ratio": 0.5,  "label": "🔸 小型（½）", "tag": "（半口）"},
    "微型": {"ratio": 0.2,  "label": "🔹 微型（⅕）", "tag": "（迷你口）"},
}

def _resolve_lot_size(price: float, size_type: str, custom_lot: int = 0) -> tuple[int, str]:
    """
    回傳 (lot_size, display_label) 依合約類型計算每口股數。
    custom_lot > 0 時優先使用（自訂模式）。
    """
    if custom_lot > 0:
        return custom_lot, f"自訂（{custom_lot:,} 股/口）"
    base   = _estimate_lot_size(price)
    info   = _SIZE_TYPE_INFO.get(size_type, _SIZE_TYPE_INFO["標準"])
    lot    = max(100, int(base * info["ratio"]))   # 最小 100 股
    label  = f"{info['label']}　{lot:,} 股/口"
    return lot, label

def _fetch_taifex_margin(stock_no: str) -> tuple[float, float]:
    """
    嘗試從 TAIFEX Open API 取得個股期貨保證金比率。
    回傳 (原始保證金比率, 維持保證金比率)。
    若 API 失敗回傳預設值（原始13.5%, 維持10.5%）。
    TAIFEX 一般個股期貨保證金介於 10%~17%，依各股波動度設定。
    """
    try:
        r = requests.get(
            "https://openapi.taifex.com.tw/v1/DailyMarketInfo",
            params={"commodity_id": f"SF{stock_no}"},   # 個股期貨代號格式
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, list) and data[0]:
                item = data[0]
                orig  = float(item.get("OriginalMargin",  0)) or 0
                maint = float(item.get("MaintenanceMargin", 0)) or 0
                if orig > 0 and maint > 0:
                    return orig, maint
    except Exception:
        pass
    return 0.135, 0.105   # 預設近似比率

def _prepare_individual_futures(
    stock_code: str,
    direction: str,
    entry_price: float,
    contracts: int,
    custom_lot: int = 0,      # 0 = 依 size_type 自動估算
    size_type:  str = "標準", # 標準 / 小型 / 微型
) -> dict:
    """
    個股期貨試算（抓標的股票現價，計算 P&L / 保證金 / ATR / 情境）。
    支援標準 / 小型（½）/ 微型（⅕）三種合約規模。
    不含 Claude 呼叫，供串流版指令使用。
    """
    yf_sym     = _stock_code_to_yf(stock_code)
    stock_no   = yf_sym.replace(".TW", "").replace(".TWO", "")

    # ── 抓股票現價 ─────────────────────────────────────────────
    quote = _fetch_quote(yf_sym)
    if not quote:
        return {"error": f"❌ 找不到股票代碼 `{stock_code}`\n格式：台股填數字（如 `2330`），美股填代碼（如 `NVDA`）"}

    curr_price   = float(quote["price"].replace(",", ""))
    stock_name   = quote.get("name", stock_code)
    pct_today    = quote.get("pct", "")
    chg_today    = quote.get("change", "")

    # ── 合約規格（依 size_type 決定每口股數）─────────────────
    lot_size, size_label = _resolve_lot_size(curr_price, size_type, custom_lot)
    is_long  = "買" in direction or "多" in direction or direction.lower() in ("long","buy")
    sign     = 1 if is_long else -1
    dir_tag  = "🔴 買進（多）" if is_long else "🟢 賣出（空）"
    currency = quote.get("currency", "TWD")

    contract_val = curr_price * lot_size          # 每口目前市值
    pnl_per_tick = lot_size * contracts           # 股/每台幣1元變動

    # ── 保證金（嘗試 TAIFEX API，失敗用估算）─────────────────
    orig_ratio, maint_ratio = _fetch_taifex_margin(stock_no)
    orig_m  = int(contract_val * orig_ratio  * contracts)
    maint_m = int(contract_val * maint_ratio * contracts)
    using_approx = (orig_ratio == 0.135)   # 是否為估算值

    # ── P&L ────────────────────────────────────────────────────
    pnl          = (curr_price - entry_price) * lot_size * contracts * sign
    pnl_tag      = f"{'🟩 獲利' if pnl >= 0 else '🟥 虧損'} {pnl:+,.0f} {currency}"

    # ── Margin Call 觸發價 ─────────────────────────────────────
    diff_per_shr = (orig_m - maint_m) / (lot_size * contracts) if lot_size > 0 else 0
    call_price   = entry_price - diff_per_shr * sign if diff_per_shr > 0 else None
    buf_cash_ind = orig_m - maint_m   # 全部口數的總緩衝金額

    # ── ATR（用股票歷史資料）──────────────────────────────────
    hist = _fetch_fut_history(yf_sym, 15)   # 沿用同一函式，Yahoo v8
    hist10 = hist[-10:] if len(hist) >= 10 else hist
    atr    = _calc_atr(hist10) if hist10 else 0
    highs  = [r["high"]  for r in hist10 if r.get("high")]
    lows   = [r["low"]   for r in hist10 if r.get("low")]
    amp_list = [(r["high"]-r["low"])/r["low"]*100
                for r in hist10 if r.get("high") and r.get("low") and r["low"]>0]
    period_hi = max(highs) if highs else None
    period_lo = min(lows)  if lows  else None
    avg_amp   = round(sum(amp_list)/len(amp_list), 1) if amp_list else 0

    # ── 組合輸出文字 ───────────────────────────────────────────
    margin_note = "（估算值，實際請至 TAIFEX 確認）" if using_approx else "（TAIFEX 即時數據）"

    lines = [
        f"## 📈 個股期貨試算 | {now_str()}",
        f"**{stock_name}（{stock_no}）** — {dir_tag}　**{contracts} 口**　｜ {size_label}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "**📋 合約資訊**",
        f"• 標的現價：**{curr_price:,.2f} {currency}**　今日漲跌：{chg_today}（{pct_today}）",
        f"• 合約規格：**{size_label}**",
        f"• {contracts} 口合計持股：**{lot_size*contracts:,} 股**",
        f"• 每股漲跌 1 元 → 損益 **{pnl_per_tick:,} 元**",
        f"• 目前合約市值（每口）：**{contract_val:,.0f} {currency}**",
        "",
        "**📊 目前損益**",
        f"• 進場價格：**{entry_price:,.2f}**",
        f"• 現價：**{curr_price:,.2f}**　→ {pnl_tag}（{(curr_price-entry_price)*sign:+.2f} 元/股）",
    ]

    if orig_m > 0:
        lines += [
            "",
            f"**💰 保證金資訊** {margin_note}",
            f"• 原始保證金（{orig_ratio:.1%}）：**{orig_m:,}** {currency}（入場須繳）",
            f"• 維持保證金（{maint_ratio:.1%}）：**{maint_m:,}** {currency}（低於此線追繳）",
            f"• 保證金緩衝：**{buf_cash_ind:,}** {currency}（約 **{diff_per_shr:,.2f}** 元/股）— 超過即觸發追繳",
        ]

    if call_price is not None:
        dir_word  = "跌破" if is_long else "突破"
        move_word = "下跌" if is_long else "上漲"
        lines += [
            "",
            "**⚠️ 追加保證金（Margin Call）分析**",
            f"• 觸發條件：虧損超過 **{buf_cash_ind:,} {currency}**（股價{move_word} **{diff_per_shr:,.2f}** 元）",
            f"• 觸發股價：{dir_word} **{call_price:,.2f}**",
            f"• 追繳金額：**{buf_cash_ind:,} {currency}**（補回至原始保證金 **{orig_m:,}** {currency}）",
            "• ⏰ 追繳期限：**T+1 收盤前**（次一交易日），逾期券商強制平倉",
            "",
            f"📉 **虧損階梯**（{dir_tag}，緩衝 {diff_per_shr:,.2f} 元/股 / {buf_cash_ind:,} {currency}）：",
        ]
        for pct_int, pct in [(25, 0.25), (50, 0.50), (75, 0.75), (100, 1.0), (150, 1.5)]:
            loss_psh  = diff_per_shr * pct
            loss_cash = buf_cash_ind  * pct
            price_at  = entry_price  - loss_psh * sign
            balance   = orig_m       - loss_cash
            if pct < 1.0:
                remain = buf_cash_ind - loss_cash
                marker = "🟡" if pct <= 0.5 else "🟠"
                status = (f"✅ 安全（距追繳還有 {remain:,.0f} {currency}）" if pct <= 0.5
                          else f"⚠️ 接近警戒（距追繳剩 {remain:,.0f} {currency}）")
            elif pct == 1.0:
                marker = "🔴"
                status = f"🚨 **追繳 {int(buf_cash_ind):,} {currency}**（T+1 截止）"
            else:
                marker = "💀"
                extra  = loss_cash - buf_cash_ind
                status = f"💀 超過追繳線 {extra:,.0f} {currency}，**強平風險**"
            lines.append(
                f"• {marker} 股價{move_word} **{loss_psh:.2f} 元**（{pct_int}%）"
                f" → 股價 **{price_at:,.2f}**　帳餘 {balance:,.0f} {currency}　{status}"
            )

    if atr > 0:
        sl_1x  = entry_price - atr * sign
        sl_15  = entry_price - atr * 1.5 * sign
        loss_1x = atr * lot_size * contracts
        lines += [
            "",
            f"**📐 停損建議（近10日 ATR = {atr:,.2f} 元）**",
            f"• 1x ATR：**{sl_1x:,.2f}**　→ 最大虧損 **{loss_1x:,.0f} 元**",
            f"• 1.5x ATR：**{sl_15:,.2f}**　→ 較寬鬆，適合波動大盤",
        ]

    if atr > 0:
        steps = [round(atr*m, 2) for m in [0.5, 1, 2, 3]]
        lines += ["", f"**💡 損益情境（ATR={atr:.2f}）**"]
        for step in steps:
            for s in [1, -1]:
                ps  = round(entry_price + step*s, 2)
                pl  = (ps - entry_price) * lot_size * contracts * sign
                arr = "⬆️" if s > 0 else "⬇️"
                mc  = call_price and ((is_long and ps <= call_price) or (not is_long and ps >= call_price))
                mct = " ⚠️Margin Call!" if mc else ""
                lines.append(f"• {arr} {s*step:+.2f}元 → 股價 {ps:,.2f}　損益 **{pl:+,.0f} 元**{mct}")

    if hist10:
        lines += ["", f"**📅 近 {len(hist10)} 個交易日行情**"]
        for row in hist10:
            amp = ((row["high"]-row["low"])/row["low"]*100
                   if row.get("high") and row.get("low") and row["low"]>0 else 0)
            lines.append(
                f"• {row['date']}　高 {row['high']:,.2f}　低 {row['low']:,.2f}"
                f"　收 {row['close']:,.2f}　振幅 {amp:.1f}%"
            )
        if period_hi and period_lo:
            lines += [
                "",
                f"• 期間最高：**{period_hi:,.2f}**　最低：**{period_lo:,.2f}**",
                f"• {len(hist10)} 日平均振幅：**{avg_amp:.1f}%**",
            ]

    # ── AI prompt ─────────────────────────────────────────────
    price_ctx = f"現價 {curr_price:,.2f}"
    sl_ctx    = f"1xATR 停損 {entry_price-atr*sign:,.2f}" if atr > 0 else ""
    hi_ctx    = f"近10日高 {period_hi:,.2f} 低 {period_lo:,.2f}" if period_hi and period_lo else ""

    ai_prompt = f"""你是一位台灣個股期貨交易風險分析師。根據以下試算數據，以繁體中文撰寫完整風險評估（禁 Markdown 表格，全程 bullet（•），500字以內）。

標的：{stock_name}（{stock_no}）
方向：{direction}（{dir_tag}）
進場：{entry_price:,.2f}　口數：{contracts} 口（每口 {lot_size:,} 股）
{price_ctx}　損益：{pnl:+,.0f} 元
ATR（近10日）：{atr:,.2f} 元　{sl_ctx}　{hi_ctx}
今日漲跌：{chg_today}（{pct_today}）　平均振幅：{avg_amp:.1f}%
原始保證金：{orig_m:,} 元（維持 {maint_m:,} 元，緩衝 {buf_cash_ind:,} 元 / {diff_per_shr:,.2f} 元/股）
Margin Call 觸發股價：{f'{call_price:,.2f}' if call_price else 'N/A'}（觸發後 T+1 需補繳 {buf_cash_ind:,} 元）

請輸出以下五節：

**📊 當前狀況評估**
• 目前盈虧方向、個股目前技術面趨勢（多/空/整理）
• 距 Margin Call 的緩衝空間（股價差＋百分比）

**📐 關鍵價位分析**
• 主要支撐位（帶數字，近10日低點/均線）
• 主要壓力位（帶數字，近10日高點/均線）
• 突破/跌破哪個價位才改變看法？

**⚡ 風險管理建議**
• 1x ATR vs 1.5x ATR 停損，哪個更適合這支股票的波動特性？
• 個股期貨特有風險：流動性、強制平倉、除息調整
• 若已盈利，移動停損的建議條件

**⚖️ 多空論點**
• 支持當前方向的 2 個最強理由（帶具體數值）
• 威脅當前方向的 2 個最大風險（個股基本面或籌碼）

**💡 操作建議**
• 短期（1-3 日）：維持/減倉/出場？觸發條件？
• 中期（5-10 日）：目標股價 → 出場計畫

> ⚠️ 以上為 AI 生成試算，保證金為估算值，不構成投資建議。"""

    return {
        "error":     None,
        "calc_text": "\n".join(lines),
        "ai_prompt": ai_prompt,
        "footer": (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "> ⚠️ 個股期貨每口股數與保證金數字為估算值，"
            "請至 [TAIFEX 個股期貨](https://www.taifex.com.tw/cht/2/stockFutures) 確認最新規格"
        ),
    }


# ── 指數 / 商品期貨試算功能 ───────────────────────────────────────

_FUT_SPEC = {
    "TX":  {"name": "臺股期貨（大台）",     "yf": "TXF=F",  "mult": 200,  "tick": 1,    "unit": "點"},
    "MX":  {"name": "小型臺股期貨（小台）", "yf": "MXF=F",  "mult": 50,   "tick": 1,    "unit": "點"},
    "TE":  {"name": "電子期貨",             "yf": None,      "mult": 4000, "tick": 0.05, "unit": "點"},
    "TF":  {"name": "金融期貨",             "yf": None,      "mult": 1000, "tick": 0.2,  "unit": "點"},
    "XIF": {"name": "非金電期貨",           "yf": None,      "mult": 200,  "tick": 1,    "unit": "點"},
    "GDF": {"name": "黃金期貨",             "yf": "GC=F",    "mult": 100,  "tick": 0.5,  "unit": "美元"},
    "BRF": {"name": "布蘭特原油期貨",       "yf": "BZ=F",    "mult": 1000, "tick": 0.01, "unit": "美元"},
}
_MARGIN_TABLE = {
    "TX":  {"orig": 170000, "maint": 130000},
    "MX":  {"orig":  43000, "maint":  33000},
    "TE":  {"orig":  84000, "maint":  64500},
    "TF":  {"orig":  41000, "maint":  31500},
    "XIF": {"orig":  42000, "maint":  32000},
    "GDF": {"orig":  27000, "maint":  20700},
    "BRF": {"orig":  27000, "maint":  20700},
}


def _calc_atr(history: list, period: int = 10) -> float:
    """計算 ATR（Average True Range），供停損建議使用"""
    trs = []
    for i in range(1, len(history)):
        h  = history[i].get("high",  0) or 0
        l  = history[i].get("low",   0) or 0
        pc = history[i - 1].get("close", 0) or 0
        if h > 0 and l > 0 and pc > 0:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    return round(sum(trs[-period:]) / min(period, len(trs)), 1)


def _fetch_fut_history(yf_symbol: str, days: int = 15) -> list:
    """抓取期貨 OHLCV（Yahoo Finance v8，不依賴 yfinance）"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
            f"?interval=1d&range={days}d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
        )
        if r.status_code != 200:
            return []
        result     = r.json()["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        q = result["indicators"]["quote"][0]
        rows = []
        for i, ts in enumerate(timestamps):
            c = q["close"][i]  if i < len(q.get("close",  [])) else None
            o = q["open"][i]   if i < len(q.get("open",   [])) else None
            h = q["high"][i]   if i < len(q.get("high",   [])) else None
            l = q["low"][i]    if i < len(q.get("low",    [])) else None
            v = q["volume"][i] if i < len(q.get("volume", [])) else None
            if c is None:
                continue
            dt = datetime.datetime.fromtimestamp(ts, tz=TW_TZ)
            rows.append({"date": dt.strftime("%m/%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
        return rows
    except Exception as e:
        print(f"期貨歷史錯誤 {yf_symbol}：{e}", flush=True)
        return []


def _fetch_spot_twii() -> float | None:
    """抓台指現貨 ^TWII 最新收盤，供期現基差計算"""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/^TWII?interval=1d&range=2d",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if r.status_code == 200:
            closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
            return closes[-1] if closes else None
    except Exception:
        pass
    return None


def _prepare_futures(symbol: str, direction: str, entry_price: float, contracts: int) -> dict:
    """
    期貨試算：抓取資料 + 組合計算結果文字 + 建立 AI prompt（不呼叫 Claude）。
    回傳 dict：
      error      → str | None
      calc_text  → 計算結果區塊（P&L / 保證金 / ATR / 情境 / 近期行情）
      ai_prompt  → Claude 分析 prompt
      footer     → 最後一行免責聲明
    """
    code = symbol.upper().strip()
    spec = _FUT_SPEC.get(code, {
        "name": code, "yf": f"{code}=F" if "=" not in code else code,
        "mult": 1, "tick": 1, "unit": "點",
    })
    margin  = _MARGIN_TABLE.get(code, {"orig": 0, "maint": 0})
    mult    = spec["mult"]
    is_long = "買" in direction or "多" in direction or direction.lower() in ("long", "buy")
    sign    = 1 if is_long else -1
    dir_tag = "🔴 買進（多）" if is_long else "🟢 賣出（空）"

    # 抓歷史
    history       = _fetch_fut_history(spec["yf"], 15) if spec.get("yf") else []
    hist10        = history[-10:] if len(history) >= 10 else history
    current_price = history[-1]["close"] if history else None

    # ATR & 近期統計
    atr      = _calc_atr(hist10) if hist10 else 0
    highs    = [r["high"]  for r in hist10 if r.get("high")]
    lows     = [r["low"]   for r in hist10 if r.get("low")]
    amp_list = [(r["high"] - r["low"]) / r["low"] * 100
                for r in hist10 if r.get("high") and r.get("low") and r["low"] > 0]
    period_hi = max(highs) if highs else None
    period_lo = min(lows)  if lows  else None
    avg_amp   = round(sum(amp_list) / len(amp_list), 1) if amp_list else 0

    # P&L 計算
    pnl         = (current_price - entry_price) * mult * contracts * sign if current_price else None
    pnl_per_pt  = mult * contracts
    orig        = margin["orig"]
    maint       = margin["maint"]
    call_thresh = (orig - maint) * contracts if orig > 0 else 0
    call_price  = (entry_price - call_thresh / (mult * contracts) * sign
                   if call_thresh > 0 and mult > 0 else None)
    # 每口緩衝空間（點數）
    buf_pts = (orig - maint) / mult if (orig > 0 and mult > 0) else 0

    # 期現基差（只對 TX/MX）
    basis_line = ""
    if code in ("TX", "MX") and current_price:
        spot = _fetch_spot_twii()
        if spot:
            basis = current_price - spot
            btype = "正價差" if basis > 0 else "逆價差"
            basis_line = f"• 台指期現基差：期貨 {current_price:,.0f} 現貨 {spot:,.0f} → **{basis:+.0f}**（{btype}）"

    pnl_tag = (f"{'🟩 獲利' if (pnl or 0) >= 0 else '🟥 虧損'} {pnl:+,.0f} 元"
               if pnl is not None else "（目前價格未取得）")

    lines = [
        f"## ⚡ 期貨試算 | {now_str()}",
        f"**{spec['name']}（{code}）** — {dir_tag}　**{contracts} 口**",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "**📋 合約資訊**",
        f"• 進場價格：**{entry_price:,.0f}**",
        f"• 合約乘數：{mult:,} 元/{spec['unit']}",
        f"• {contracts} 口每點損益：**{pnl_per_pt:,} 元**",
    ]

    if current_price:
        move = (current_price - entry_price) * sign
        lines += [
            "",
            "**📊 目前損益**",
            f"• 目前價格：**{current_price:,.0f}**",
            f"• {pnl_tag}（{move:+.0f} {spec['unit']}）",
        ]
    if basis_line:
        lines.append(basis_line)

    if orig > 0:
        orig_total  = orig  * contracts
        maint_total = maint * contracts
        lines += [
            "",
            "**💰 保證金資訊**（參考值，實際請至 taifex.com.tw 確認）",
            f"• 原始保證金：**{orig:,}** 元/口 × {contracts} 口 = **{orig_total:,}** 元（入場須繳）",
            f"• 維持保證金：**{maint:,}** 元/口 × {contracts} 口 = **{maint_total:,}** 元（低於此線追繳）",
            f"• 保證金緩衝：**{call_thresh:,}** 元（約 **{buf_pts:.0f}** {spec['unit']}）— 超過此幅度即觸發追繳",
        ]

    if call_price is not None:
        dir_word   = "跌破" if is_long else "突破"
        move_word  = "下跌" if is_long else "上漲"
        orig_total = orig  * contracts
        lines += [
            "",
            "**⚠️ 追加保證金（Margin Call）分析**",
            f"• 觸發條件：虧損超過 **{call_thresh:,} 元**（{move_word} **{buf_pts:.0f} {spec['unit']}**）",
            f"• 觸發價位：{dir_word} **{call_price:,.0f}**",
            f"• 追繳金額：**{call_thresh:,} 元**（補回至原始保證金 **{orig_total:,}** 元）",
            "• ⏰ 追繳期限：**T+1 收盤前**（次一交易日），逾期券商強制平倉",
            "",
            f"📉 **虧損階梯**（{dir_tag}，緩衝 {buf_pts:.0f} {spec['unit']} / {call_thresh:,} 元）：",
        ]
        for pct_int, pct in [(25, 0.25), (50, 0.50), (75, 0.75), (100, 1.0), (150, 1.5)]:
            loss_pts  = buf_pts    * pct
            loss_cash = call_thresh * pct
            price_at  = entry_price - loss_pts * sign
            balance   = orig_total - loss_cash
            if pct < 1.0:
                remain = call_thresh - loss_cash
                marker = "🟡" if pct <= 0.5 else "🟠"
                status = (f"✅ 安全（距追繳還有 {remain:,.0f} 元）" if pct <= 0.5
                          else f"⚠️ 接近警戒（距追繳剩 {remain:,.0f} 元）")
            elif pct == 1.0:
                marker = "🔴"
                status = f"🚨 **追繳 {int(call_thresh):,} 元**（T+1 截止）"
            else:
                marker = "💀"
                extra  = loss_cash - call_thresh
                status = f"💀 超過追繳線 {extra:,.0f} 元，**強平風險**"
            lines.append(
                f"• {marker} {move_word} **{loss_pts:.0f} {spec['unit']}**（{pct_int}%）"
                f" → 價格 **{price_at:,.0f}**　帳餘 {balance:,.0f} 元　{status}"
            )

    if atr > 0:
        sl_1x = entry_price - atr * sign
        sl_15 = entry_price - atr * 1.5 * sign
        max_loss_1x = atr * mult * contracts
        lines += [
            "",
            f"**📐 停損建議（近10日 ATR = {atr:,.0f} {spec['unit']}）**",
            f"• 1x ATR 停損：**{sl_1x:,.0f}**　→ 最大虧損 **{max_loss_1x:,.0f} 元**（{'偏緊' if atr / (entry_price or 1) < 0.005 else '合理'}）",
            f"• 1.5x ATR 停損：**{sl_15:,.0f}**　→ 給市場更多震盪空間",
        ]

    if current_price and atr > 0:
        steps = [int(atr * m) for m in [0.5, 1, 2, 3]]
        lines += ["", f"**💡 損益情境（基於 ATR={atr:.0f}）**"]
        for step in steps:
            for s in [1, -1]:
                price_s = entry_price + step * s
                pnl_s   = (price_s - entry_price) * mult * contracts * sign
                arrow   = "⬆️" if s > 0 else "⬇️"
                hit_mc  = call_price and ((is_long and price_s <= call_price) or (not is_long and price_s >= call_price))
                mc_tag  = " ⚠️Margin Call!" if hit_mc else ""
                lines.append(f"• {arrow} {s*step:+.0f}{spec['unit']} → 價格 {price_s:,.0f}　損益 **{pnl_s:+,.0f} 元**{mc_tag}")

    if hist10:
        lines += ["", f"**📅 近 {len(hist10)} 個交易日行情**"]
        for row in hist10:
            amp = ((row["high"] - row["low"]) / row["low"] * 100
                   if row.get("high") and row.get("low") and row["low"] > 0 else 0)
            lines.append(
                f"• {row['date']}　高 {row['high']:,.0f}　低 {row['low']:,.0f}"
                f"　收 {row['close']:,.0f}　振幅 {amp:.1f}%"
            )
        if period_hi and period_lo:
            lines += [
                "",
                f"• 期間最高：**{period_hi:,.0f}**　最低：**{period_lo:,.0f}**",
                f"• {len(hist10)} 日平均振幅：**{avg_amp:.1f}%**",
            ]

    # ── 組建 AI prompt ─────────────────────────────────────────
    price_ctx = f"目前價 {current_price:,.0f}" if current_price else "（目前價格未取得）"
    pnl_ctx   = f"損益 {pnl:+,.0f} 元（{(current_price - entry_price)*sign:+.0f} 點）" if pnl is not None else ""
    sl_ctx    = f"1xATR 停損 {entry_price - atr*sign:,.0f}" if atr > 0 else ""
    hi_ctx    = f"期間高 {period_hi:,.0f} 低 {period_lo:,.0f}" if period_hi and period_lo else ""

    ai_prompt = f"""你是一位期貨交易風險分析師。根據以下試算數據，以繁體中文撰寫完整風險評估（禁 Markdown 表格，全程 bullet（•），500字以內）。

商品：{spec['name']}（{code}）
方向：{direction}（{dir_tag}）
進場：{entry_price:,.0f}　口數：{contracts} 口
{price_ctx}　{pnl_ctx}
ATR（近10日）：{atr:,.0f} {spec['unit']}
{sl_ctx}　{hi_ctx}
平均振幅：{avg_amp:.1f}%
原始保證金：{orig:,} 元/口（{contracts} 口合計 {orig*contracts:,} 元）
維持保證金：{maint:,} 元/口　緩衝：{buf_pts:.0f} {spec['unit']} / {call_thresh:,} 元
Margin Call 觸發價：{f'{call_price:,.0f}' if call_price else 'N/A'}（觸發後 T+1 需補繳 {call_thresh:,} 元）

請輸出以下五節：

**📊 當前狀況評估**
• 目前盈虧方向、倉位風險程度（低/中/高）
• 距 Margin Call 的緩衝空間（點數＋百分比）

**📐 關鍵價位分析**
• 主要支撐位（帶數字，依近期行情）
• 主要壓力位（帶數字）
• 突破/跌破哪個價位才改變看法？

**⚡ 風險管理建議**
• 停損設置：1x ATR vs 1.5x ATR，哪個更適合目前市場波動度？
• 若已盈利，考慮移動停損的條件是什麼？
• 加碼條件（若確認趨勢，可在哪個位置加碼？）

**⚖️ 多空論點**
• 支持當前方向的 2 個最強理由（帶具體數值）
• 威脅當前方向的 2 個最大風險

**💡 操作建議**
• 短期（1-3 日）：維持/減倉/出場？觸發條件是什麼？
• 中期（5-10 日）：目標價位 → 出場計畫

> ⚠️ 以上為 AI 生成試算，保證金數字為參考值，不構成投資建議。"""

    return {
        "error":     None,
        "calc_text": "\n".join(lines),
        "ai_prompt": ai_prompt,
        "footer":    "━━━━━━━━━━━━━━━━━━━━━━\n> ⚠️ 保證金數字為參考值，請至 [TAIFEX](https://www.taifex.com.tw/cht/2/parame) 查詢最新",
    }


def _do_futures(symbol: str, direction: str, entry_price: float, contracts: int) -> str:
    """同步版期貨試算（向下相容，含 Claude 呼叫）"""
    data = _prepare_futures(symbol, direction, entry_price, contracts)
    if data["error"]:
        return data["error"]
    ai_text = _claude_call(data["ai_prompt"], max_tokens=700)
    return (
        data["calc_text"]
        + "\n\n**🤖 AI 風險評估**\n" + ai_text
        + "\n\n" + data["footer"]
    )


def _update_warrant_prices_bg():
    """
    每日收盤後（16:30）批量更新曾被查詢的權證收盤價並計算 BS。
    使用批量 MIS 大幅降低 HTTP 請求數。
    """
    from warrant import _fetch_basic_all, _fetch_basic_all_tpex, _fetch_mis_batch
    from warrant import _ratio as _get_ratio, _strike, _expiry, _wtype, _days_to_expiry
    from warrant_bs import full_calc
    tracked = warrant_db.get_tracked_warrants(limit=300)
    if not tracked:
        print("⏰ 收盤更新：無追蹤權證", flush=True)
        return

    basic_all = _fetch_basic_all() + _fetch_basic_all_tpex()
    basic_map = {r.get("權證代號", ""): r for r in basic_all}

    # 收集所有需查詢的代號（權證 + 標的）
    warrant_pairs: list[tuple[str, str]] = []
    stock_pairs:   list[tuple[str, str]] = []
    seen_stocks: set = set()
    for item in tracked:
        code     = item["code"]
        und_code = item["und_code"]
        row      = basic_map.get(code, {})
        exchange = "otc" if row.get("_source") == "tpex" else "tw"
        warrant_pairs.append((code, exchange))
        if und_code and und_code not in seen_stocks:
            seen_stocks.add(und_code)
            stock_pairs.append((und_code, "tw"))

    print(f"⏰ 批量更新收盤價：{len(tracked)} 檔（批量 MIS）…", flush=True)
    mis_w_map = _fetch_mis_batch(warrant_pairs)
    mis_s_map = _fetch_mis_batch(stock_pairs)

    ok_cnt = bs_cnt = fail_cnt = 0
    for item in tracked:
        code     = item["code"]
        und_code = item["und_code"]
        ratio_f  = item["ratio_f"] or 1.0
        try:
            mis_w = mis_w_map.get(code, {})
            mis_s = mis_s_map.get(und_code, {}) if und_code else {}
            w_p = float(mis_w.get("price") or 0)
            s_p = float(mis_s.get("price") or 0)
            if w_p <= 0:
                fail_cnt += 1
                continue

            # 計算 BS 並存入快取
            bs_rec: dict = {}
            row = basic_map.get(code, {})
            if s_p > 0 and row:
                try:
                    strike_s = _strike(row)
                    days_r   = _days_to_expiry(_expiry(row))
                    is_call  = "購" in _wtype(row)
                    if strike_s and days_r > 0:
                        bs_rec = full_calc(s_p, float(strike_s), days_r, w_p, ratio_f, is_call)
                        if bs_rec:
                            warrant_db.save_bs_cache(code, bs_rec)
                            bs_cnt += 1
                except Exception:
                    pass

            warrant_db.record_price(
                code, und_code, w_p, s_p, ratio_f,
                iv_bs=bs_rec.get("iv"), delta_bs=bs_rec.get("delta"),
                premium_pct=bs_rec.get("premium_pct"),
            )
            ok_cnt += 1
        except Exception:
            fail_cnt += 1

    print(f"⏰ 收盤更新完成：價格 {ok_cnt} / BS快取 {bs_cnt} / 失敗 {fail_cnt}", flush=True)


def _daily_scheduler():
    """在 TW 08:00 / 15:00 / 22:00 觸發 GitHub Actions 日報；16:30 更新權證收盤價"""
    _triggered   = set()
    _trigger_ts  = {}   # key → datetime，防止重啟後雙實例在 10 分鐘內重複觸發
    DAILY_HOURS   = {8: "morning", 15: "afternoon", 22: "evening"}
    PODCAST_HOURS = {9, 21}

    # ── 啟動保護：隨機等待 0-8 秒，避免重啟時兩個實例同時觸發 ──
    import random
    time.sleep(random.uniform(0, 8))

    while True:
        try:
            now = datetime.datetime.now(TW_TZ)
            key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"

            if now.minute < 5 and key not in _triggered:
                # 額外保護：同一 key 10 分鐘內不重複觸發（防重啟雙實例）
                last_ts = _trigger_ts.get(key)
                too_soon = last_ts and (now - last_ts).total_seconds() < 600

                if not too_soon:
                    if now.hour in DAILY_HOURS and GITHUB_PAT:
                        ok, err = _trigger_workflow("stock-daily.yml", {})
                        status  = '✅' if ok else f'❌ {err}'
                        print(f"⏰ 觸發{DAILY_HOURS[now.hour]}日報 {status}", flush=True)
                        _triggered.add(key)
                        if ok:
                            _trigger_ts[key] = now
                    elif now.hour in PODCAST_HOURS and GITHUB_PAT:
                        ok, err = _trigger_workflow("podcast.yml", {})
                        status  = '✅' if ok else f'❌ {err}'
                        print(f"⏰ 觸發Podcast追蹤 {status}", flush=True)
                        _triggered.add(key)
                        if ok:
                            _trigger_ts[key] = now
                else:
                    print(f"⏰ 排程：{key} 10分鐘內已觸發，跳過（防重啟雙送）", flush=True)
                    _triggered.add(key)

            # 16:30 收盤後：批量更新價格 + 同步生命週期狀態
            price_key = f"price-{now.strftime('%Y-%m-%d')}"
            if now.hour == 16 and 30 <= now.minute < 35 and price_key not in _triggered:
                _triggered.add(price_key)
                threading.Thread(target=_update_warrant_prices_bg, daemon=True).start()
                threading.Thread(target=warrant_db.sync_warrant_status, daemon=True).start()

            # 17:00 盤後（週一～五）：執行策略 Agent（選股掃描 + 信號更新 + Discord 明牌）
            strategy_key = f"strategy-{now.strftime('%Y-%m-%d')}"
            if (now.hour == 17 and now.minute < 5
                    and now.weekday() < 5          # 0=Mon … 4=Fri
                    and strategy_key not in _triggered):
                _triggered.add(strategy_key)
                print("⏰ 啟動策略 Agent（盤後選股）", flush=True)
                threading.Thread(target=_run_strategy_agent, daemon=True).start()

            # 每天 00:00 清除已觸發記錄
            if now.hour == 0 and now.minute == 0:
                _triggered.clear()
                _trigger_ts.clear()
        except Exception as e:
            print(f"⚠️ 排程例外：{e}", flush=True)
        time.sleep(30)

threading.Thread(target=_daily_scheduler, daemon=True).start()
print("⏰ 排程啟動（日報 08/15/22:00、Podcast 09/21:00 TW）", flush=True)

# ── Discord Bot ───────────────────────────────────────────────

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

def _split_messages(text: str, limit: int = 1900) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks

def _is_query_channel(interaction: discord.Interaction) -> bool:
    """檢查是否在允許查詢的頻道（未設定 QUERY_CHANNEL_ID 則允許全部）"""
    if not QUERY_CHANNEL_ID:
        return True
    return str(interaction.channel_id) == QUERY_CHANNEL_ID

@tree.command(name="查股", description="查詢股票即時行情與 AI 深度分析")
@app_commands.describe(
    symbol="股票代碼（台股：2330.TW｜美股：NVDA｜指數：^TWII）",
    name="股票名稱（選填，如：台積電）",
)
async def cmd_查股(interaction: discord.Interaction, symbol: str, name: str = ""):
    print(f"⚡ /查股 收到：{symbol}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /查股。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /查股 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /查股 defer 失敗：{e}", flush=True)
        return
    try:
        # ── Phase 1：抓取資料（同步，不阻塞 event loop）─────────────
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, _prepare_analysis, symbol.strip().upper(), name.strip()
        )
        if data["error"]:
            await interaction.followup.send(data["error"])
            return

        # ── Phase 2：傳送初始訊息 + 串流 AI 分析 ──────────────────
        msg = await interaction.followup.send(
            data["header"] + "🔍 AI 分析中，請稍候... ▌"
        )
        full_text = await _stream_to_discord(
            data["prompt"], msg,
            prefix=data["header"], max_tokens=2400
        )

        # ── Phase 3：最終確認 + 處理超長訊息 ─────────────────────
        chunks = _split_messages(full_text)
        await msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)

    except Exception as e:
        print(f"❌ /查股 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass

# ── /新聞 ────────────────────────────────────────────────────────

@tree.command(name="新聞", description="最新財經新聞 + AI 摘要（台股 / 美股 / 國際）")
@app_commands.describe(市場="選擇新聞市場")
@app_commands.choices(市場=[
    app_commands.Choice(name="📈 台股", value="台股"),
    app_commands.Choice(name="🇺🇸 美股", value="美股"),
    app_commands.Choice(name="🌏 國際", value="國際"),
])
async def cmd_新聞(interaction: discord.Interaction, 市場: app_commands.Choice[str]):
    mtype = 市場.value
    print(f"⚡ /新聞 收到：{mtype}", flush=True)
    try:
        await interaction.response.defer()
    except Exception:
        return
    try:
        loop   = asyncio.get_running_loop()
        titles = await loop.run_in_executor(None, _fetch_market_news, mtype)
        if not titles:
            await interaction.followup.send(f"❌ 目前無法取得 {mtype} 新聞，請稍後再試。")
            return

        news_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
        prompt = f"""你是財經新聞分析師。以下是最新的{mtype}財經新聞標題，請用繁體中文寫簡明摘要（500字以內）。

禁用 Markdown 表格，全程 bullet（• 開頭）。
{mtype}慣例：🔴=漲/利多，🟢=跌/利空。

【新聞標題】
{news_text}

請輸出兩節：

**🔥 重要事件（3-5條最重要的）**
• 事件 → 影響方向 → 重要性（高/中/低）

**📊 市場影響研判**
• 整體偏多或偏空？重點受惠/受壓族群？

> ⚠️ AI 生成摘要，不構成投資建議。"""

        header = (
            f"## 📰 {mtype}最新財經新聞摘要 | {now_str()}\n"
            f"{'─' * 28}\n\n"
        )
        msg = await interaction.followup.send(header + "🔍 AI 分析新聞中... ▌")
        full_text = await _stream_to_discord(prompt, msg, prefix=header, max_tokens=900)
        await msg.edit(content=full_text[:2000])

    except Exception as e:
        print(f"❌ /新聞 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 取得新聞失敗：{str(e)[:150]}")
        except Exception:
            pass


# ── /自選股新增 / /自選股刪除 / /自選股清單 ───────────────────────

@tree.command(name="自選股新增", description="新增股票到個人自選股清單（最多 20 支）")
@app_commands.describe(
    symbol="股票代碼（台股：2330.TW｜美股：NVDA）",
    name="股票名稱（選填，如：台積電）",
)
async def cmd_自選股新增(interaction: discord.Interaction, symbol: str, name: str = ""):
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id or "dm")
    ok, msg_text = watchlist_db.add_stock(uid, gid, symbol.strip().upper(), name.strip())
    await interaction.response.send_message(msg_text, ephemeral=True)


@tree.command(name="自選股刪除", description="從個人自選股清單刪除股票")
@app_commands.describe(symbol="股票代碼（例：2330.TW）")
async def cmd_自選股刪除(interaction: discord.Interaction, symbol: str):
    uid = str(interaction.user.id)
    gid = str(interaction.guild_id or "dm")
    ok, msg_text = watchlist_db.remove_stock(uid, gid, symbol.strip().upper())
    await interaction.response.send_message(msg_text, ephemeral=True)


@tree.command(name="自選股清單", description="查看個人自選股清單")
async def cmd_自選股清單(interaction: discord.Interaction):
    uid    = str(interaction.user.id)
    gid    = str(interaction.guild_id or "dm")
    stocks = watchlist_db.get_watchlist(uid, gid)
    if not stocks:
        await interaction.response.send_message(
            "📋 你的自選股清單是空的。\n"
            "使用 `/自選股新增 代碼 名稱` 開始新增（例：`/自選股新增 2330.TW 台積電`）。",
            ephemeral=True,
        )
        return
    lines = [f"📋 **你的自選股清單**（{len(stocks)}/{watchlist_db.MAX_STOCKS}）\n"]
    for s in stocks:
        date = s["added_at"][:10]
        label = f"  {s['name']}" if s["name"] else ""
        lines.append(f"• `{s['symbol']}`{label}  _(加入：{date})_")
    lines.append("\n> 使用 `/自選股刪除 代碼` 移除，`/查股 代碼` 查詢個股")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ── /自選股分析（個人版）────────────────────────────────────────

@tree.command(name="自選股分析", description="逐支分析你個人自選股清單中的股票（串流顯示）")
async def cmd_自選股分析(interaction: discord.Interaction):
    uid    = str(interaction.user.id)
    gid    = str(interaction.guild_id or "dm")
    stocks = watchlist_db.get_watchlist(uid, gid)

    if not stocks:
        await interaction.response.send_message(
            "📋 你的自選股清單是空的！\n"
            "先用 `/自選股新增 代碼 名稱` 加入股票，再執行 `/自選股分析`。",
            ephemeral=True,
        )
        return

    print(f"⚡ /自選股分析 收到：{uid}，共 {len(stocks)} 支", flush=True)
    try:
        await interaction.response.defer()
    except Exception:
        return

    n = len(stocks)
    est = n * 15   # 每支約 15 秒
    await interaction.followup.send(
        f"📊 開始分析你的 **{n}** 支自選股（預計約 **{est//60}分{est%60}秒**）\n"
        f"結果將逐支出現在下方 ↓"
    )

    loop = asyncio.get_running_loop()
    for i, stock in enumerate(stocks, 1):
        sym  = stock["symbol"]
        name = stock["name"]
        label = name or sym

        # 進度佔位訊息
        prog_msg = await interaction.channel.send(
            f"🔍 分析中（{i}/{n}）：**{label}**（`{sym}`）▌"
        )

        # 抓取資料（同步，在 executor 執行）
        data = await loop.run_in_executor(None, _prepare_analysis, sym, name)

        if data["error"]:
            await prog_msg.edit(content=f"❌ **{sym}**：{data['error'][:200]}")
            await asyncio.sleep(1)
            continue

        # 串流 Claude 分析
        full_text = await _stream_to_discord(
            data["prompt"], prog_msg,
            prefix=data["header"], max_tokens=1800,
        )

        # 最終確認：處理超長訊息
        chunks = _split_messages(full_text)
        await prog_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)

        # 節流：避免 Discord rate limit + Claude rate limit
        await asyncio.sleep(2)

    await interaction.channel.send(
        f"✅ **個人自選股分析完成**（共 {n} 支）| {now_str()}\n"
        "> 資料來源：Yahoo Finance + Claude AI　⚠️ 不構成投資建議"
    )


# ── /看板（自選股即時看板）─────────────────────────────────────────

@tree.command(name="看板", description="自選股即時看板：一次顯示所有自選股的漲跌 / 成交量 / 強弱排行")
async def cmd_看板(interaction: discord.Interaction):
    uid    = str(interaction.user.id)
    gid    = str(interaction.guild_id or "dm")
    stocks = watchlist_db.get_watchlist(uid, gid)

    if not stocks:
        await interaction.response.send_message(
            "📋 你的自選股清單是空的！\n先用 `/自選股新增` 加入股票，再執行 `/看板`。",
            ephemeral=True,
        )
        return

    print(f"⚡ /看板 收到：{uid}，共 {len(stocks)} 支", flush=True)
    try:
        await interaction.response.defer()
    except Exception:
        return

    # 並行抓取所有報價（executor 裡跑，避免阻塞 event loop）
    import time as _t
    loop   = asyncio.get_running_loop()

    async def _fetch_one(sym: str, name: str):
        q = await loop.run_in_executor(None, _fetch_quote, sym)
        return sym, name, q

    tasks  = [_fetch_one(s["symbol"], s["name"]) for s in stocks]
    results = await asyncio.gather(*tasks)

    # 整理、排序（漲幅由高到低）
    rows = []
    for sym, name, q in results:
        if q:
            label = name or sym
            pct_f = 0.0
            try:
                pct_f = float(q["pct"].replace("%", "").replace("+", ""))
            except Exception:
                pass
            rows.append((pct_f, sym, label, q))
        else:
            rows.append((-999, sym, name or sym, None))

    rows.sort(key=lambda x: x[0], reverse=True)

    # 組合顯示文字
    ts    = now_str()
    lines = [f"## 📊 自選股即時看板 | {ts}", "━━━━━━━━━━━━━━━━━━━━━━", ""]

    winners, losers, flat = 0, 0, 0
    for pct_f, sym, label, q in rows:
        if q is None:
            lines.append(f"• ❓ **{label}**（`{sym}`）— 無法取得報價")
            continue
        arrow = "▲" if pct_f > 0 else ("▼" if pct_f < 0 else "─")
        if pct_f > 0:
            winners += 1
        elif pct_f < 0:
            losers  += 1
        else:
            flat    += 1
        vol_str = f"量 {q['volume']}" if q.get("volume") and q["volume"] != "N/A" else ""
        bar = "█" * min(10, max(1, abs(int(pct_f * 2))))   # 視覺強度條
        lines.append(
            f"• {q['emoji']} **{label}**（`{sym}`）"
            f"　{q['price']}　{arrow} **{q['pct']}**　{vol_str}"
        )

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 上漲 **{winners}** 支　📉 下跌 **{losers}** 支　⬛ 持平 **{flat}** 支",
        f"> 資料來源：Yahoo Finance　｜　共 {len(rows)} 支自選股",
    ]

    # 分割傳送
    full = "\n".join(lines)
    chunks = _split_messages(full)
    await interaction.followup.send(chunks[0])
    for chunk in chunks[1:]:
        await interaction.channel.send(chunk)


# ── /自選股 ──────────────────────────────────────────────────────

@tree.command(name="自選股", description="觸發完整自選股分析（結果約 2 分鐘後出現）")
async def cmd_自選股(interaction: discord.Interaction):
    print("⚡ /自選股 收到", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /自選股 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /自選股 defer 失敗：{e}", flush=True)
        return
    try:
        loop       = asyncio.get_running_loop()
        ok, errmsg = await loop.run_in_executor(None, _trigger_workflow, "query_watchlist.yml", {})
        if ok:
            await interaction.followup.send("⏳ 自選股分析啟動！約 **2 分鐘**後報告會出現在自選股頻道。")
        elif not GITHUB_PAT:
            await interaction.followup.send(
                "⚠️ 尚未設定 `GITHUB_PAT`，無法觸發自選股分析。\n"
                "請在 Bot 環境變數中加入 `GITHUB_PAT`（需 `workflow` 權限的 GitHub PAT）。"
            )
        else:
            await interaction.followup.send(f"❌ 觸發失敗：`{errmsg}`")
    except Exception as e:
        print(f"❌ /自選股 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

# ── 個股期貨指令 ─────────────────────────────────────────────

@tree.command(name="個股期", description="個股期貨試算：損益 / 保證金 / ATR停損 / 情境 / AI評估（支援標準/小型/微型）")
@app_commands.describe(
    stock       = "台股代碼（如 2330）或美股（NVDA）",
    direction   = "買進（多）或 賣出（空）",
    entry_price = "進場股價（如 895.0）",
    contracts   = "口數（預設 1）",
    size_type   = "合約規模：標準／小型（½口）／微型（⅕口）",
    lot_size    = "自訂每口股數（選填，覆蓋 size_type；0=自動）",
)
@app_commands.choices(direction=[
    app_commands.Choice(name="買進（多）", value="買進"),
    app_commands.Choice(name="賣出（空）", value="賣出"),
])
@app_commands.choices(size_type=[
    app_commands.Choice(name="標準",         value="標準"),
    app_commands.Choice(name="🔸 小型（½）", value="小型"),
    app_commands.Choice(name="🔹 微型（⅕）", value="微型"),
])
async def cmd_個股期(
    interaction: discord.Interaction,
    stock:       str,
    direction:   app_commands.Choice[str],
    entry_price: float,
    contracts:   int = 1,
    size_type:   app_commands.Choice[str] = None,
    lot_size:    int = 0,
):
    dir_str  = direction.value
    size_str = size_type.value if size_type else "標準"
    print(f"⚡ /個股期 收到：{stock} {dir_str} @{entry_price} {contracts}口 {size_str} lot={lot_size}", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        return
    except Exception as e:
        print(f"❌ /個股期 defer 失敗：{e}", flush=True)
        return
    try:
        # Phase 1：抓資料 + 計算（~2s）
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, _prepare_individual_futures,
            stock.strip(), dir_str, entry_price, max(1, contracts), lot_size, size_str,
        )
        if data.get("error"):
            await interaction.followup.send(data["error"])
            return

        # Phase 2：立刻顯示計算結果
        calc_chunks = _split_messages(data["calc_text"])
        await interaction.followup.send(calc_chunks[0])
        for chunk in calc_chunks[1:]:
            await interaction.channel.send(chunk)

        # Phase 3：串流 AI 風險評估
        ai_msg = await interaction.channel.send("**🤖 AI 風險評估**\n🔍 分析中... ▌")
        ai_text = await _stream_to_discord(data["ai_prompt"], ai_msg, max_tokens=700)
        await ai_msg.edit(content="**🤖 AI 風險評估**\n" + ai_text)
        await interaction.channel.send(data["footer"])

    except Exception as e:
        print(f"❌ /個股期 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 試算失敗：{str(e)[:150]}")
        except Exception:
            pass


# ── 期貨指令 ─────────────────────────────────────────────────

@tree.command(name="期貨", description="期貨試算：損益 / 保證金 / ATR停損 / 情境分析 / AI評估")
@app_commands.describe(
    symbol      = "期貨代碼（TX=大台、MX=小台、TE=電子、TF=金融、GDF=黃金、BRF=布蘭特油）",
    direction   = "買進（多）或 賣出（空）",
    entry_price = "進場價格（數字，如：21500）",
    contracts   = "口數（預設 1）",
)
@app_commands.choices(direction=[
    app_commands.Choice(name="買進（多）", value="買進"),
    app_commands.Choice(name="賣出（空）", value="賣出"),
])
async def cmd_期貨(
    interaction: discord.Interaction,
    symbol:      str,
    direction:   app_commands.Choice[str],
    entry_price: float,
    contracts:   int = 1,
):
    dir_str = direction.value
    print(f"⚡ /期貨 收到：{symbol} {dir_str} @{entry_price} {contracts}口", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /期貨 互動已過期，忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /期貨 defer 失敗：{e}", flush=True)
        return
    try:
        # ── Phase 1：抓取資料 + 計算（同步，~2s）─────────────────
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, _prepare_futures,
            symbol.strip().upper(), dir_str, entry_price, max(1, contracts)
        )
        if data.get("error"):
            await interaction.followup.send(data["error"])
            return

        # ── Phase 2：立刻送出計算結果（不用等 AI）──────────────
        calc_chunks = _split_messages(data["calc_text"])
        msg = await interaction.followup.send(calc_chunks[0])
        for chunk in calc_chunks[1:]:
            await interaction.channel.send(chunk)

        # ── Phase 3：串流 AI 風險評估 ─────────────────────────
        ai_msg = await interaction.channel.send("**🤖 AI 風險評估**\n🔍 分析中... ▌")
        ai_text = await _stream_to_discord(
            data["ai_prompt"], ai_msg, max_tokens=700
        )
        # 最終訊息
        await ai_msg.edit(content="**🤖 AI 風險評估**\n" + ai_text)
        await interaction.channel.send(data["footer"])

    except Exception as e:
        print(f"❌ /期貨 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 試算失敗：{str(e)[:150]}")
        except Exception:
            pass


# ── 權證指令 ─────────────────────────────────────────────────

def _do_warrant_search(stock_input: str) -> str:
    code, zh_name = lookup_stock(stock_input)
    summary = search_warrants(stock_input)
    if summary.startswith("❌"):
        return summary

    prompt = f"""你是一位台灣認購權證專家。以下是 {zh_name}（{code}）目前市場上的認購權證候選清單，已依綜合評分由高到低排序，全部剩餘天數 > 90 天。

【重要格式規則】
- 禁 Markdown 表格（禁用 | 符號製表）
- 全程 bullet（• 開頭）
- 每項評估必須帶入實際數值，不可泛泛而談

【認購權證候選清單】
{summary}

請以繁體中文撰寫（800字以內）：

**🏆 推薦排名分析**
對每一檔逐一評估，格式：

**第N名　代號 名稱**（若有【評級】請顯示）
• 到期日：X 天剩餘
• 溢價率 X% → 評估（偏高/合理/偏低）
• 槓桿 X 倍 → 評估（甜蜜點 5-15 倍）
• 今日成交 XXX 張 → 流動性（佳/>50萬/普通/差）
• ✅ 優勢：最吸引人的 2 個具體理由
• ⚠️ 注意：1-2 個風險點

**💡 綜合建議**
• 首選：第幾名？理由（綜合到期/溢價/流動性/槓桿）
• 短線（1-2週）首選：哪檔？為什麼？
• 中線（1個月以上）首選：哪檔？為什麼？
• 進場前必確認：溢價率上限 + 流動性要求 + 停損設置

> ⚠️ AI 生成，不構成投資建議。交易前請自行確認最新報價。"""

    analysis = _claude_call(prompt, max_tokens=2000)
    header = (
        f"## 🔍 {zh_name}（{code}）認購權證推薦 | {now_str()}\n"
        f"{'─' * 28}\n\n"
    )
    return header + analysis


def _do_warrant_analyze(warrant_code: str) -> str:
    target_summary, similar_summary = analyze_warrant(warrant_code)
    if target_summary.startswith("❌"):
        return target_summary

    prompt = f"""你是一位台灣認購權證專家，請根據以下實際數據進行逐項評估。

【重要格式規則】
- 禁 Markdown 表格（禁用 | 符號製表）
- 全程 bullet（• 開頭）
- 每項評估必須帶入實際數值，不可省略；資料顯示「—」才寫「資料不足」

【目標權證資料】
{target_summary}

【同標的其他認購權證（供比較）】
{similar_summary}

請以繁體中文撰寫（900字以內）：

**📋 逐項數據評估**

**① 剩餘天數**
• 到期日：【填入】，距今【填入】天 → 時間充裕度：（充裕/普通/偏短）+ 一句時間價值衰退說明

**② 價內外狀態**
• 履約價【填入】元 vs 標的現價【填入】元
• 目前【價內/價外】X%，標的需漲 X% 才損益兩平

**③ 溢價率**
• 溢價率【填入】%（若無法計算請說明原因）
• 評估：（偏高>8%/合理3-8%/偏低<3%） → 對報酬的實際影響

**④ 槓桿倍數**
• 槓桿【填入】倍（若估算請標注）→ 5-15 倍為甜蜜點，評估報酬風險比

**⑤ 流動性（成交量）**
• 今日成交【填入】張 → 佳（>50萬）/ 普通（5-50萬）/ 差（<5萬）
• 出場難易度說明

**⑥ 發行規模**
• 發行量【填入】仟單位 → 供應充足度評估

> ℹ️ Delta / IV / 有效槓桿：TWSE 公開資料未提供，請向發行商查詢報價系統

**✅ 主要優勢**（2-3點，每點帶實際數值）

**⚠️ 主要風險**（2-3點，每點帶實際數值）

**🔄 同標的選項比較**
若有其他同標的認購權證，對每一檔逐一 bullet 說明：
• 代號 名稱：剩餘天數 X 天、溢價率 X%、成交 XXX 張 → 優於/劣於目標權證的理由
若無同標的：說明「目前市場無其他同標的認購權證」，不製作空表格

**⭐ 綜合評級**
• 若資料顯示【評級 A+/A/B/C/D】直接引用；否則自行評定 A/B/C/D
• 一句話說明評定依據

> ⚠️ AI 生成，不構成投資建議。"""

    analysis = _claude_call(prompt, max_tokens=2200)
    header = (
        f"## 🎯 權證 `{warrant_code.upper()}` 深度分析 | {now_str()}\n"
        f"{'─' * 28}\n\n"
    )
    return header + analysis


@tree.command(name="查權證", description="輸入股票代碼或名稱，列出推薦認購權證（含排名與優缺點）")
@app_commands.describe(stock="股票代碼或中文名（例：2330 或 台積電）")
async def cmd_查權證(interaction: discord.Interaction, stock: str):
    print(f"⚡ /查權證 收到：{stock}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /查權證。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /查權證 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /查權證 defer 失敗：{e}", flush=True)
        return
    try:
        loop         = asyncio.get_running_loop()
        progress_msg = await interaction.followup.send("🔍 正在蒐集資料，請稍候…")
        result       = await loop.run_in_executor(None, _do_warrant_search, stock.strip())
        chunks       = _split_messages(result)
        await progress_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查權證 執行失敗：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass


@tree.command(name="分析權證", description="輸入權證代號，深度分析並推薦同標的替代選項")
@app_commands.describe(code="權證代號（例：038542）")
async def cmd_分析權證(interaction: discord.Interaction, code: str):
    print(f"⚡ /分析權證 收到：{code}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /分析權證。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /分析權證 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /分析權證 defer 失敗：{e}", flush=True)
        return
    try:
        loop         = asyncio.get_running_loop()
        progress_msg = await interaction.followup.send("🔍 正在分析中，請稍候…")
        result       = await loop.run_in_executor(None, _do_warrant_analyze, code.strip())
        chunks       = _split_messages(result)
        await progress_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /分析權證 執行失敗：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 分析失敗：{str(e)[:150]}")
        except Exception:
            pass


# ── /明牌 ────────────────────────────────────────────────────────

@tree.command(name="明牌", description="策略 Agent 每日選股明牌 + 勝率統計")
@app_commands.describe(查詢="選擇查詢模式")
@app_commands.choices(查詢=[
    app_commands.Choice(name="📊 今日信號",   value="today"),
    app_commands.Choice(name="📈 勝率統計",   value="winrate"),
    app_commands.Choice(name="🚀 立即執行",   value="run"),
])
async def cmd_明牌(interaction: discord.Interaction, 查詢: app_commands.Choice[str] = None):
    mode = 查詢.value if 查詢 else "today"
    print(f"⚡ /明牌 收到：mode={mode}", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /明牌 互動已過期，忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /明牌 defer 失敗：{e}", flush=True)
        return

    try:
        loop = asyncio.get_running_loop()

        if mode == "today":
            result = await loop.run_in_executor(None, _get_strategy_today)
            chunks = _split_messages(result)
            await interaction.followup.send(chunks[0])
            for chunk in chunks[1:]:
                await interaction.channel.send(chunk)

        elif mode == "winrate":
            result = await loop.run_in_executor(None, _get_strategy_winrate)
            chunks = _split_messages(result)
            await interaction.followup.send(chunks[0])
            for chunk in chunks[1:]:
                await interaction.channel.send(chunk)

        elif mode == "run":
            # 手動觸發策略 Agent（非同步背景執行，不等待完成）
            await interaction.followup.send(
                "🚀 **策略 Agent 啟動中...**\n"
                "掃描 ETF 成分股 + 更新歷史勝率，約 **2～3 分鐘**後明牌結果會發送至明牌頻道。\n"
                "（或直接用 `📊 今日信號` 查詢結果）"
            )
            threading.Thread(target=_run_strategy_agent, daemon=True).start()

    except Exception as e:
        print(f"❌ /明牌 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:150]}")
        except Exception:
            pass


@client.event
async def on_ready():
    await tree.sync()
    cmds = [c.name for c in tree.get_commands()]
    print(f"✅ Bot 啟動：{client.user}（ID: {client.user.id}）", flush=True)
    print(f"   指令：{cmds}", flush=True)
    print(f"   ANTHROPIC_API_KEY：{'✅ 已設定' if ANTHROPIC_API_KEY else '❌ 未設定'}", flush=True)
    print(f"   GITHUB_PAT：{'✅ 已設定' if GITHUB_PAT else '⚠️  未設定（/自選股 不可用）'}", flush=True)
    print(f"   指令：/查股 /新聞 /看板 /自選股新增 /自選股刪除 /自選股清單 /自選股分析 /自選股 /個股期 /期貨 /查權證 /分析權證 /明牌", flush=True)
    warrant_db.init_db()     # 建立/確認權證資料庫表格
    watchlist_db.init_db()   # 建立/確認個人自選股資料庫表格
    # 策略 Agent DB 初始化（Railway Volume 路徑由 SIGNAL_DB_PATH 環境變數控制）
    try:
        from strategy_agent.signal_db import init_db as _strategy_init_db
        _strategy_init_db()
    except Exception as e:
        print(f"⚠️ 策略 DB 初始化失敗（非致命）：{e}", flush=True)
    prewarm_cache()          # 背景預熱快取（優先用 DB，DB 過期才打 API）

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動", flush=True)
else:
    client.run(DISCORD_BOT_TOKEN)
