"""
stock_daily.py
台股 / 美股 / 國際新聞爬蟲 + Claude AI 分析 + Discord 傳送

使用免費 RSS（Yahoo Finance / Google News / MoneyDJ / 鉅亨網）

定時規則（台灣時間）：
  08:00 → 整理前天 22:00 ~ 當天 08:00 的新聞
  15:00 → 整理當天 08:00 ~ 15:00 的新聞
  22:00 → 整理當天 15:00 ~ 22:00 的新聞 + 自選股分析

自選股清單：直接編輯 watchlist.txt，格式為「代碼 名稱」，一行一檔
  例如：
    2330.TW 台積電
    NVDA    輝達

使用方式：
  python stock_daily.py           ← 立即執行一次（測試用）
  python stock_daily.py --schedule ← 啟動排程
"""

import os
import sys
import re
import json
import time
import datetime
import warnings
import feedparser
import requests
import schedule
import anthropic
import pytz
import io

try:
    import zhconv
    def _s2tw(text: str) -> str:
        return zhconv.convert(text, "zh-tw")
except ImportError:
    def _s2tw(text: str) -> str:
        return text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from technical_indicators import get_full_indicators, format_indicators_for_prompt

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JIN10_TOKEN       = os.environ.get("JIN10_TOKEN", "")
REPORT_SESSION    = os.environ.get("REPORT_SESSION", "auto")   # morning / afternoon / evening / auto
RUN_STATE_FILE    = "run_state.json"

DISCORD_TW       = os.environ.get("DISCORD_TW",       "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW")
DISCORD_US       = os.environ.get("DISCORD_US",       "https://discord.com/api/webhooks/1508308789537800242/y6l377lQUOovmgh19He7wn5DlPN2_k19B2ksGVpmErCV46K-o7XSRnoXM97DDkpmglOP")
DISCORD_GLOBAL   = os.environ.get("DISCORD_GLOBAL",   "https://discord.com/api/webhooks/1507953174512668902/QsKOUt5afzwQYfbQQeGi8Tza2-gkLKUJaP-B03lWEyX9C5ops59NuGHLJCK7a8UC9N5-")
DISCORD_WATCHLIST= os.environ.get("DISCORD_WATCHLIST","https://discord.com/api/webhooks/1508115009924894744/rlYl9lqindxWlauA4Ie4UJsWbneXM2nQZbuCa8_0XWKLvF3gpku5LONetISQ-MHzJhl1")

TAIPEI_TZ = pytz.timezone("Asia/Taipei")
TW_TZ     = TAIPEI_TZ

print(f"🕐 程式啟動時間（台北）：{datetime.datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

# ─────────────────────────────────────────────────────────────
# 自選股：從 watchlist.txt 讀取
# ─────────────────────────────────────────────────────────────

def load_watchlist(path: str = None) -> list:
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "自選股", "watchlist.txt")
    """
    從 watchlist.txt 讀取自選股清單。
    格式：每行「代碼 名稱」，# 開頭為註解，空行忽略。
    例：
        2330.TW 台積電
        NVDA    輝達
        # 這是註解
    """
    result = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    result.append({"symbol": parts[0].upper(), "name": parts[1].strip()})
                elif len(parts) == 1:
                    result.append({"symbol": parts[0].upper(), "name": parts[0].upper()})
        print(f"✅ 已讀取自選股清單：{len(result)} 檔")
        for s in result:
            print(f"   {s['symbol']} {s['name']}")
    except FileNotFoundError:
        print("⚠️ 找不到 watchlist.txt，使用預設清單")
        result = [
            {"symbol": "2330.TW", "name": "台積電"},
            {"symbol": "2454.TW", "name": "聯發科"},
            {"symbol": "NVDA",    "name": "輝達"},
            {"symbol": "AAPL",    "name": "蘋果"},
            {"symbol": "TSLA",    "name": "特斯拉"},
            {"symbol": "^TWII",   "name": "加權指數"},
        ]
    except Exception as e:
        print(f"❌ 讀取 watchlist.txt 失敗：{e}，使用預設清單")
        result = [
            {"symbol": "2330.TW", "name": "台積電"},
            {"symbol": "NVDA",    "name": "輝達"},
        ]
    return result

WATCHLIST = load_watchlist()

# ─────────────────────────────────────────────────────────────
# 時段判斷
# ─────────────────────────────────────────────────────────────

def get_session_info() -> dict:
    _sessions = {
        "morning":   {"label": "盤前早報", "period": "前日 22:00 ～ 今日 08:00", "emoji": "🌅", "start_h": 22, "end_h": 8},
        "afternoon": {"label": "盤中午報", "period": "今日 08:00 ～ 15:00",       "emoji": "☀️", "start_h": 8,  "end_h": 15},
        "evening":   {"label": "盤後晚報", "period": "今日 15:00 ～ 22:00",       "emoji": "🌙", "start_h": 15, "end_h": 22},
    }
    # Workflow 明確傳入時段 → 直接使用（不受延遲影響）
    if REPORT_SESSION in _sessions:
        return _sessions[REPORT_SESSION]
    # 自動偵測（手動觸發 / 本機測試）
    # 加寬邊界：每個時段各有 ±1h 緩衝，避免 GitHub Actions 延遲誤判
    h = datetime.datetime.now(TW_TZ).hour
    if 7 <= h < 14:
        return _sessions["morning"]
    elif 14 <= h < 21:
        return _sessions["afternoon"]
    else:
        return _sessions["evening"]

def is_weekend() -> bool:
    return datetime.datetime.now(TW_TZ).weekday() >= 5

def load_run_state() -> dict:
    try:
        with open(RUN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_run_state(state: dict):
    cutoff = (datetime.datetime.now(TW_TZ) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    state  = {k: v for k, v in state.items() if k >= cutoff}
    with open(RUN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def now_tw() -> datetime.datetime:
    return datetime.datetime.now(TAIPEI_TZ)

def now_str() -> str:
    return now_tw().strftime("%Y-%m-%d %H:%M")

# ─────────────────────────────────────────────────────────────
# Claude AI
# ─────────────────────────────────────────────────────────────

def claude_call(prompt: str, max_tokens: int = 1500) -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"
    try:
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-opus-4-5",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=60,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        else:
            print(f"❌ Claude API 錯誤：HTTP {r.status_code} {r.text[:200]}")
            return f"AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"AI 分析暫時無法使用（{str(e)[:100]}）"

# ─────────────────────────────────────────────────────────────
# 大盤數據（Yahoo Finance）
# ─────────────────────────────────────────────────────────────

def fetch_yahoo(symbol: str, name: str = "") -> dict:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}
        data   = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        volumes= result["indicators"]["quote"][0].get("volume", [])
        timestamps = result.get("timestamp", [])
        closes = [c for c in closes if c is not None]
        meta   = result.get("meta", {})
        currency = meta.get("currency", "")

        stale = False
        if timestamps and is_weekend():
            last_dt = datetime.datetime.fromtimestamp(timestamps[-1], tz=TW_TZ)
            if last_dt.weekday() >= 5 or (now_tw() - last_dt).days >= 1:
                stale = True

        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg = curr - prev
            pct = chg / prev * 100
            emoji = "🔴" if pct < 0 else "🟢"
            vols  = [v for v in volumes if v is not None]
            ma5   = sum(closes[-5:])  / min(5,  len(closes)) if closes else None
            ma10  = sum(closes[-10:]) / min(10, len(closes)) if closes else None
            return {
                "name":     name or symbol,
                "price":    f"{curr:,.2f}",
                "price_f":  curr,
                "change":   f"{chg:+.2f}",
                "pct":      f"{pct:+.2f}%",
                "emoji":    emoji,
                "currency": currency,
                "stale":    stale,
                "volume":   f"{vols[-1]:,.0f}" if vols else "N/A",
                "ma5":      f"{ma5:,.2f}"  if ma5  else "N/A",
                "ma10":     f"{ma10:,.2f}" if ma10 else "N/A",
                "closes":   closes,
            }
        return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}
    except Exception as e:
        print(f"Yahoo 錯誤 {symbol}：{e}")
        return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}

def fmt_quote(q: dict) -> str:
    stale_tag = " ⚠️未更新（上次交易日）" if q.get("stale") else ""
    return f"{q['emoji']} {q['price']} ({q['pct']}){stale_tag}"

# ─────────────────────────────────────────────────────────────
# 加密貨幣（CoinGecko）
# ─────────────────────────────────────────────────────────────

def fetch_crypto() -> dict:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"},
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
                results[symbol] = {"price": f"${price:,.2f}", "pct": f"{pct:+.2f}%", "emoji": emoji}
        return results
    except Exception as e:
        print(f"CoinGecko 錯誤：{e}")
        return {}

# ─────────────────────────────────────────────────────────────
# RSS 新聞爬蟲
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# 金十數據（Jin10 MCP API）— 國際即時新聞 + 財經行事曆
# ─────────────────────────────────────────────────────────────

_jin10_session_id: str = ""

def _jin10_post(payload: dict, session_id: str = "") -> tuple:
    """返回 (result_dict, new_session_id)"""
    headers = {"Content-Type": "application/json"}
    if JIN10_TOKEN:
        headers["Authorization"] = f"Bearer {JIN10_TOKEN}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        r = requests.post("https://mcp.jin10.com/mcp", headers=headers,
                          json=payload, timeout=20)
        # Session ID 在 HTTP response header 裡
        new_sid = r.headers.get("Mcp-Session-Id", session_id)
        # 解析 SSE 格式（data: {...} 行）
        for line in r.content.decode("utf-8").splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].strip())
                    if "result" in obj:
                        return obj["result"], new_sid
                except Exception:
                    pass
        try:
            return r.json().get("result", {}), new_sid
        except Exception:
            return {}, new_sid
    except Exception as e:
        print(f"金十 API 錯誤：{e}")
        return {}, session_id

def jin10_initialize() -> str:
    global _jin10_session_id
    if not JIN10_TOKEN:
        return ""
    _, new_sid = _jin10_post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "stock-bot", "version": "1.0"},
        }
    })
    _jin10_session_id = new_sid
    print(f"  金十 Session ID：{_jin10_session_id[:16]}..." if _jin10_session_id else "  金十 Session ID：未取得")
    return _jin10_session_id

def jin10_call(tool: str, args: dict) -> list:
    if not JIN10_TOKEN:
        return []
    result, _ = _jin10_post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": args}
    }, session_id=_jin10_session_id)
    # result.content[0].text 是一個 JSON 字串，需再次 parse
    try:
        content_list = result.get("content", [])
        if result.get("isError") and content_list:
            print(f"  金十工具錯誤（{tool}）：{content_list[0].get('text','')[:120]}")
            return []
        text = content_list[0].get("text", "") if content_list else ""
        parsed = json.loads(text)
        data = parsed.get("data", [])
        # list_flash 回傳 {"data": {"has_more": ..., "items": [...]}}
        if isinstance(data, dict):
            return data.get("items", [])
        # list_calendar 回傳 {"data": [...]}
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  金十解析錯誤（{tool}）：{e}")
        return []

def fetch_jin10_flash(session_info: dict) -> list:
    """取得本時段的金十國際即時快訊"""
    if not JIN10_TOKEN:
        return []
    try:
        items = jin10_call("list_flash", {})
        if not items:
            print("  金十快訊：無資料")
            return []

        now     = now_tw()
        start_h = session_info["start_h"]
        end_h   = session_info["end_h"]
        if start_h < end_h:
            ref      = (now - datetime.timedelta(days=1)) if (now.hour < 6 and start_h == 15) else now
            start_dt = ref.replace(hour=start_h, minute=0, second=0, microsecond=0)
            end_dt   = ref.replace(hour=end_h,   minute=0, second=0, microsecond=0)
        else:
            yesterday = now - datetime.timedelta(days=1)
            start_dt  = yesterday.replace(hour=start_h, minute=0, second=0, microsecond=0)
            end_dt    = now.replace(hour=end_h, minute=0, second=0, microsecond=0)

        result = []
        for item in items:
            content = _s2tw(item.get("content") or "")
            time_raw = item.get("time") or ""
            if not content:
                continue
            try:
                pub_dt = datetime.datetime.fromisoformat(
                    time_raw.replace("Z", "+00:00")).astimezone(TW_TZ)
            except Exception:
                pub_dt = None
            if pub_dt and not (start_dt <= pub_dt <= end_dt):
                continue
            result.append({
                "time":    pub_dt.strftime("%H:%M") if pub_dt else "",
                "title":   content[:150],
            })
        print(f"  金十快訊：取得 {len(result)} 則（時段內）")
        return result[:15]
    except Exception as e:
        print(f"  金十快訊錯誤：{e}")
        return []

def fetch_jin10_calendar() -> list:
    """取得今日重要財經事件行事曆（★★以上）"""
    if not JIN10_TOKEN:
        return []
    try:
        items = jin10_call("list_calendar", {})
        result = []
        for item in items:
            star     = int(item.get("star", 0) or 0)
            if star < 2:
                continue
            event    = _s2tw(item.get("title") or "")
            time_s   = item.get("pub_time") or item.get("time") or ""
            actual   = item.get("actual") or ""
            forecast = item.get("consensus") or ""
            affect   = _s2tw(item.get("affect_txt") or "")
            if event:
                result.append({
                    "event":    event[:60],
                    "time":     time_s,
                    "actual":   actual,
                    "forecast": forecast,
                    "affect":   affect,
                    "star":     star,
                })
        print(f"  金十行事曆：取得 {len(result)} 筆（★★以上）")
        return result[:10]
    except Exception as e:
        print(f"  金十行事曆錯誤：{e}")
        return []

def build_jin10_text(flash: list, calendar: list) -> str:
    """格式化金十數據為 Claude prompt 文字"""
    lines = []
    if flash:
        lines.append("【金十數據 即時快訊】")
        for f in flash:
            t = f"[{f['time']}] " if f["time"] else ""
            lines.append(f"• {t}{f['title']}")
    if calendar:
        lines.append("\n【今日重要財經事件】")
        for c in calendar:
            stars        = "★" * min(c["star"], 3)
            actual_tag   = f"公布={c['actual']}" if c["actual"] else ""
            forecast_tag = f"預期={c['forecast']}" if c["forecast"] else ""
            affect_tag   = c.get("affect", "")
            vals = "　".join(t for t in [actual_tag, forecast_tag, affect_tag] if t)
            lines.append(f"{stars} [{c['time']}] {c['event']}"
                         + (f"　{vals}" if vals else ""))
    return "\n".join(lines)

def build_jin10_discord_block(flash: list, calendar: list) -> str:
    """格式化金十數據為 Discord Embed 顯示文字"""
    parts = []
    if flash:
        news_lines = []
        for f in flash:
            t = f"`{f['time']}` " if f["time"] else ""
            news_lines.append(f"• {t}{f['title']}")
        parts.append("**📡 金十數據 即時快訊**\n" + "\n".join(news_lines))
    if calendar:
        cal_lines = []
        for c in calendar:
            stars = "⭐" * min(c["star"], 3)
            vals  = "　".join(v for v in [
                (f"公布 `{c['actual']}`"   if c["actual"]   else ""),
                (f"預期 `{c['forecast']}`" if c["forecast"] else ""),
                (c.get("affect", "")),
            ] if v)
            cal_lines.append(f"{stars} `{c['time']}` {c['event']}"
                             + (f" — {vals}" if vals else ""))
        parts.append("**📅 今日重要財經事件**\n" + "\n".join(cal_lines))
    return "\n\n".join(parts) if parts else ""

def build_jin10_discord_message(flash: list, calendar: list) -> str:
    """格式化金十數據為獨立 Discord 訊息（繁體中文大字體版）"""
    parts = []
    if flash:
        lines = ["## 📡 金十數據 即時快訊"]
        for f in flash:
            t = f"`{f['time']}` " if f["time"] else ""
            lines.append(f"**{t}{f['title']}**")
        parts.append("\n".join(lines))
    if calendar:
        lines = ["## 📅 今日重要財經事件"]
        for c in calendar:
            stars = "⭐" * min(c["star"], 3)
            vals = "  ".join(v for v in [
                (f"公布 `{c['actual']}`"   if c["actual"]   else ""),
                (f"預期 `{c['forecast']}`" if c["forecast"] else ""),
                (c.get("affect", "")),
            ] if v)
            lines.append(f"{stars} **`{c['time']}`** {c['event']}"
                         + (f" — {vals}" if vals else ""))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else ""

import json as _json_module
# 讓金十解析用 json
json = _json_module

RSS_FEEDS = {
    "tw": [
        "https://news.cnyes.com/rss/cat/tw_stock",
        "https://tw.stock.yahoo.com/rss",
        "https://news.google.com/rss/search?q=台股+股市&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=台灣+財經+科技股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://www.moneydj.com/rss/news.aspx?svc=NW&cat=MB",
    ],
    "us": [
        "https://news.google.com/rss/search?q=US+stock+market+Wall+Street&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=NASDAQ+S%26P500+Fed+earnings&hl=en-US&gl=US&ceid=US:en",
        "https://finance.yahoo.com/rss/topstories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    ],
    "global": [
        "https://news.google.com/rss/search?q=global+economy+Fed+inflation+oil&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=geopolitics+trade+war+OPEC&hl=en-US&gl=US&ceid=US:en",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],
}

def parse_rss_date(entry) -> datetime.datetime | None:
    for attr in ["published_parsed", "updated_parsed"]:
        t = getattr(entry, attr, None)
        if t:
            try:
                dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
                return dt.astimezone(TW_TZ)
            except Exception:
                pass
    return None

def fetch_rss(url: str, limit: int = 10) -> list:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        feed = feedparser.parse(url, request_headers=headers)
        articles = []
        for entry in feed.entries[:limit]:
            title   = getattr(entry, "title",   "").strip()
            link    = getattr(entry, "link",    "#").strip()
            summary = getattr(entry, "summary", "").strip()
            summary = re.sub(r"<[^>]+>", "", summary)[:200]
            pub_dt  = parse_rss_date(entry)
            if title and len(title) > 5:
                articles.append({"title": title, "url": link, "summary": summary, "pub_dt": pub_dt})
        return articles
    except Exception as e:
        print(f"RSS 錯誤 {url[:60]}：{e}")
        return []

def fetch_all_news(category: str, session_info: dict) -> list:
    feeds        = RSS_FEEDS.get(category, [])
    all_articles = []
    seen_titles  = set()

    for url in feeds:
        for a in fetch_rss(url, limit=15):
            key = re.sub(r"\s+", "", a["title"].lower())[:30]
            if key not in seen_titles:
                seen_titles.add(key)
                all_articles.append(a)
        time.sleep(0.3)

    now     = now_tw()
    start_h = session_info["start_h"]
    end_h   = session_info["end_h"]

    if start_h < end_h:
        # 正常時段（如 8→15、15→22）
        # 若已過午夜（h<6）且執行的是盤後晚報，新聞窗口應指向前一天 15→22
        ref = (now - datetime.timedelta(days=1)) if (now.hour < 6 and start_h == 15) else now
        start_dt = ref.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt   = ref.replace(hour=end_h,   minute=0, second=0, microsecond=0)
    else:
        # 跨日時段（22→8）
        yesterday = now - datetime.timedelta(days=1)
        start_dt  = yesterday.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt    = now.replace(hour=end_h, minute=0, second=0, microsecond=0)

    filtered = [a for a in all_articles if a["pub_dt"] and start_dt <= a["pub_dt"] <= end_dt]
    no_time  = [a for a in all_articles if not a["pub_dt"]]
    result   = filtered + no_time
    result.sort(key=lambda x: x["pub_dt"] or datetime.datetime.min.replace(tzinfo=TW_TZ), reverse=True)
    return result[:15]

# ─────────────────────────────────────────────────────────────
# Claude 分析 — 台股 / 美股 / 國際
# ─────────────────────────────────────────────────────────────

def build_news_text(articles: list, limit: int = 12) -> str:
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        pub  = a["pub_dt"].strftime("%m/%d %H:%M") if a["pub_dt"] else ""
        line = f"{i}. [{pub}] {a['title']}"
        if a.get("summary"):
            line += f"\n   摘要：{a['summary'][:100]}"
        lines.append(line)
    return "\n".join(lines) if lines else "（本時段暫無新聞）"

def analyze_tw_news(articles, session_info, market_data) -> str:
    twii = market_data.get("twii", {})
    wknd = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    prompt = f"""你是一位資深台股分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{wknd}

【台灣加權指數】{fmt_quote(twii) if twii else 'N/A'}

【本時段台股重大新聞】
{build_news_text(articles)}

請以繁體中文撰寫台股分析日報：
1. **市場概況**（2-3句）
2. **重點個股利多/利空分析**（Markdown 表格：| 個股名稱（代號） | 方向 | 關鍵事件 | 影響評估 |，至少 3-5 檔）
3. **類股動態**（表格：| 類股 | 趨勢 | 主要驅動因素 |）
4. **操作建議**（2-3點，每點以 • 開頭）
5. **風險提示**（1-2句）"""
    return claude_call(prompt, max_tokens=1800)

def analyze_us_news(articles, session_info, market_data) -> str:
    dji  = market_data.get("dji",  {})
    ixic = market_data.get("ixic", {})
    gspc = market_data.get("gspc", {})
    wknd = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    prompt = f"""你是一位資深美股分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{wknd}

【美股大盤】
- 道瓊：{fmt_quote(dji) if dji else 'N/A'}
- 納斯達克：{fmt_quote(ixic) if ixic else 'N/A'}
- S&P 500：{fmt_quote(gspc) if gspc else 'N/A'}

【本時段美股重大新聞】
{build_news_text(articles)}

請以繁體中文撰寫美股分析日報：
1. **市場概況**（2-3句）
2. **重點個股利多/利空分析**（Markdown 表格，至少 3-5 檔）
3. **產業板塊輪動**（表格：| 板塊 | 表現 | 主要驅動因素 |）
4. **Fed 政策與總經觀察**（2-3句）
5. **操作建議**（2-3點，每點以 • 開頭）"""
    return claude_call(prompt, max_tokens=1800)

def analyze_global_news(articles, session_info, market_data, jin10_text: str = "") -> str:
    vix   = market_data.get("vix",   {})
    gold  = market_data.get("gold",  {})
    oil   = market_data.get("oil",   {})
    dxy   = market_data.get("dxy",   {})
    us10y = market_data.get("us10y", {})
    crypto= market_data.get("crypto",{})
    btc   = crypto.get("BTC", {})
    eth   = crypto.get("ETH", {})
    wknd  = "\n⚠️ 今日為週末，部分指標為上次交易日數值。" if is_weekend() else ""
    prompt = f"""你是一位資深國際財經分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{wknd}

【全球關鍵指標】
- VIX：{fmt_quote(vix) if vix else 'N/A'}
- 美債10Y：{fmt_quote(us10y) if us10y else 'N/A'}
- 美元指數DXY：{fmt_quote(dxy) if dxy else 'N/A'}
- 黃金：{fmt_quote(gold) if gold else 'N/A'}
- 原油(WTI)：{fmt_quote(oil) if oil else 'N/A'}
- BTC：{btc.get('emoji','') + ' ' + btc.get('price','N/A') + ' (' + btc.get('pct','N/A') + ')' if btc else 'N/A'}
- ETH：{eth.get('emoji','') + ' ' + eth.get('price','N/A') + ' (' + eth.get('pct','N/A') + ')' if eth else 'N/A'}

【本時段國際重大新聞（RSS）】
{build_news_text(articles)}

{jin10_text if jin10_text else ''}

請以繁體中文撰寫國際財經分析日報（優先參考金十即時快訊，再結合 RSS 新聞）：
1. **全球市場情緒**（2-3句）
2. **重大地緣政治與總經事件**（表格：| 事件 | 影響資產 | 利多/利空 | 影響程度 |，至少 3-5 個）
3. **大宗商品與加密貨幣**（表格：| 品項 | 價格 | 漲跌 | 關鍵驅動 |）
4. **對台股/亞股的影響**（2-3點，每點以 • 開頭）
5. **本週重要財經數據預告**（若有，結合行事曆資料）"""
    return claude_call(prompt, max_tokens=2000)

def analyze_jin10(flash: list, calendar: list, session_info: dict) -> str:
    """針對金十數據進行專屬 AI 分析"""
    if not flash and not calendar:
        return ""
    jin10_text = build_jin10_text(flash, calendar)
    prompt = f"""你是一位資深國際財經分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。

以下為金十數據最新即時快訊與財經行事曆：

{jin10_text}

請以**繁體中文**撰寫金十數據分析摘要（500字以內）：
1. **重大事件解讀**（針對最重要的2-3個事件，分析其市場影響）
2. **市場情緒判斷**（整體市場情緒方向，並說明原因）
3. **對台股／亞股影響**（2-3點，每點以 • 開頭）
4. **今日需關注財經數據**（若行事曆有重要數據請標出時間）"""
    return claude_call(prompt, max_tokens=900)

# ─────────────────────────────────────────────────────────────
# 自選股分析（22:00 執行）
# ─────────────────────────────────────────────────────────────

def fetch_stock_news(symbol: str, name: str) -> str:
    """多源爬取個股新聞（Google News + 鉅亨網）"""
    titles = []
    seen   = set()

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
        stock_no = symbol.upper().replace(".TW", "")
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' 台股')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        time.sleep(0.2)
        _add(f"https://news.cnyes.com/rss/cat/tw_stock_news")   # 鉅亨 台股新聞
        time.sleep(0.2)
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(stock_no)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' stock earnings')}&hl=en-US&gl=US&ceid=US:en")
        time.sleep(0.2)
        _add(f"https://finance.yahoo.com/rss/headline?s={symbol}")  # Yahoo Finance 個股新聞
        time.sleep(0.2)
        _add(f"https://feeds.content.dowjones.io/public/rss/mw_topstories")

    return "\n".join(titles[:8]) if titles else "（暫無相關新聞）"

def analyze_single_stock(symbol: str, name: str, quote: dict, news_ctx: str = "") -> str:
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    stale_note = "⚠️ 注意：今日為週末，以下為上次交易日收盤數據。\n" if quote.get("stale") else ""

    # 取得完整技術指標
    print(f"    計算技術指標 {symbol}...")
    ind = get_full_indicators(symbol)
    indicators_text = format_indicators_for_prompt(ind)

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析。

{stale_note}【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote.get('price', 'N/A')} {quote.get('currency', '')}
- 漲跌幅：{quote.get('pct', 'N/A')}（{quote.get('change', 'N/A')}）
- {price_history}

【技術指標（實際計算值）】
{indicators_text}

【近期相關新聞（多方查證）】
{news_ctx if news_ctx else '（暫無相關新聞）'}

請以繁體中文撰寫分析報告（900 字以內），依據上方**實際技術指標數值**進行判斷，不要憑空猜測：

📊 **近期走勢**（2-3句，說明目前價格與均線關係）

🔍 **技術面分析**
- 均線多空排列與趨勢
- MACD 動能（依據實際數值判斷多空）
- RSI 位置與超買超賣
- KDJ 訊號（是否出現交叉）
- 乖離率（是否過度偏離）
- 成交量狀況（量能配合度）
- 融資券動向（若有台股資料）：融資增減意涵、融券回補壓力
- 三大法人動向（若有台股資料）：外資/投信/自營商買賣超判讀

📰 **基本面亮點**（結合近期新聞，2-3點）

🎯 **短期預測（1-2週）**
- 目標價區間：...
- 可能走向：...

💡 **操作建議**
- 多方進場條件：...
- 空方或觀望條件：...
- 停損參考：...

⚠️ 本報告由 AI 生成，僅供參考，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1400)

def run_watchlist_report():
    """執行自選股日報（每日 22:00）"""
    # 重新讀取最新的 watchlist.txt（讓用戶修改後立即生效）
    watchlist = load_watchlist()

    print(f"\n📈 執行自選股分析（共 {len(watchlist)} 檔）...")
    session_info  = get_session_info()
    ts            = now_str()
    weekend_banner= "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    send_embed(DISCORD_WATCHLIST, {
        "title":       f"📊 自選股日報 {session_info['emoji']} {session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}\n共 **{len(watchlist)}** 檔自選股，由 Claude AI 生成，不構成投資建議。",
        "color":       0xF39C12,
        "footer":      {"text": "資料來源：Yahoo Finance + Google News + Claude AI"},
    })
    time.sleep(1.2)

    for stock in watchlist:
        symbol = stock["symbol"]
        name   = stock["name"]
        print(f"  分析 {name}（{symbol}）...")

        quote    = fetch_yahoo(symbol, name)
        print(f"  價格：{fmt_quote(quote)}")

        news_ctx = fetch_stock_news(symbol, name)
        time.sleep(0.5)

        analysis = analyze_single_stock(symbol, name, quote, news_ctx)
        time.sleep(1)

        stale_tag = " ⚠️未更新" if quote.get("stale") else ""
        send_discord_message(
            DISCORD_WATCHLIST,
            f"## {quote.get('emoji','📊')} **{name}（{symbol}）** — {quote.get('price','N/A')} ({quote.get('pct','N/A')}){stale_tag}\n\n{analysis}"
        )
        time.sleep(1.5)

    print("  ✅ 自選股分析完成")

# ─────────────────────────────────────────────────────────────
# Discord 傳送
# ─────────────────────────────────────────────────────────────

MAX_EMBED_FIELD = 1024
MAX_CONTENT     = 2000

def truncate(text: str, limit: int = MAX_EMBED_FIELD) -> str:
    if not text or not text.strip():
        return "暫無資料"
    text = text.strip()
    return text[:limit - 3] + "..." if len(text) > limit else text

def send_discord_message(webhook_url: str, content: str) -> bool:
    if not content or not content.strip():
        return False
    chunks  = []
    lines   = content.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > MAX_CONTENT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    success = True
    for chunk in chunks:
        try:
            r = requests.post(webhook_url, json={"content": chunk}, timeout=15)
            if r.status_code not in [200, 204]:
                print(f"❌ Discord 傳送失敗：{r.status_code} {r.text[:200]}")
                success = False
            else:
                print(f"✅ Discord 傳送成功（{len(chunk)} 字）")
        except Exception as e:
            print(f"❌ Discord 傳送錯誤：{e}")
            success = False
        time.sleep(1.0)
    return success

def send_embed(webhook_url: str, embed: dict) -> bool:
    for field in embed.get("fields", []):
        field["value"] = truncate(field.get("value", ""), MAX_EMBED_FIELD)
    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        if r.status_code in [200, 204]:
            print(f"✅ Embed 發送成功：{embed.get('title', '')[:40]}")
            return True
        else:
            print(f"❌ Embed 發送失敗：{r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Embed 發送錯誤：{e}")
        return False

def build_news_links(articles: list, limit: int = 8) -> str:
    if not articles:
        return "暫無新聞"
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        title    = a["title"][:50]
        url      = a.get("url", "#")
        pub      = a["pub_dt"].strftime("%H:%M") if a["pub_dt"] else ""
        time_tag = f"`{pub}` " if pub else ""
        lines.append(f"{i}. {time_tag}[{title}]({url})")
    return "\n".join(lines)

def build_market_table(market_data: dict) -> str:
    mapping = [
        ("twii",  "🇹🇼 台灣加權"),
        ("dji",   "🇺🇸 道瓊"),
        ("ixic",  "📊 納斯達克"),
        ("gspc",  "📈 S&P 500"),
        ("vix",   "😱 VIX"),
        ("us10y", "🏦 美債10Y"),
        ("dxy",   "💵 美元指數"),
        ("gold",  "🥇 黃金"),
        ("oil",   "🛢️ 原油(WTI)"),
    ]
    rows = []
    for key, label in mapping:
        q = market_data.get(key, {})
        if q and q.get("price") != "N/A":
            stale = " ⚠️未更新" if q.get("stale") else ""
            rows.append(f"{label}: {q['emoji']} **{q['price']}** ({q['pct']}){stale}")
    return "\n".join(rows) if rows else "暫無數據"

# ─────────────────────────────────────────────────────────────
# 主執行函式
# ─────────────────────────────────────────────────────────────

def run_report():
    print("=" * 65)
    session_info  = get_session_info()
    today         = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    run_state     = load_run_state()

    # 防重複：同一時段當天已成功執行過就跳過
    if session_info["label"] in run_state.get(today, []):
        print(f"⏭️  {session_info['label']} 今日已執行（{today}），跳過重複執行")
        print("=" * 65)
        return

    weekend_note  = "（週末版）" if is_weekend() else ""
    print(f"🚀 股市日報啟動 — {now_str()} {weekend_note}")
    print(f"📅 本次時段：{session_info['emoji']} {session_info['label']} ({session_info['period']})")
    print("=" * 65)

    # 1. 大盤數據
    print("\n📊 抓取大盤數據...")
    market_data = {}
    symbols = {
        "twii":  ("^TWII",     "台灣加權"),
        "dji":   ("^DJI",      "道瓊"),
        "ixic":  ("^IXIC",     "納斯達克"),
        "gspc":  ("^GSPC",     "S&P 500"),
        "vix":   ("^VIX",      "VIX"),
        "us10y": ("^TNX",      "美債10Y"),
        "dxy":   ("DX-Y.NYB",  "美元指數"),
        "gold":  ("GC=F",      "黃金"),
        "oil":   ("CL=F",      "原油"),
    }
    for key, (sym, name) in symbols.items():
        market_data[key] = fetch_yahoo(sym, name)
        stale_tag = " ⚠️未更新" if market_data[key].get("stale") else ""
        print(f"  {name}: {fmt_quote(market_data[key])}{stale_tag}")
        time.sleep(0.2)

    # 2. 加密貨幣
    print("\n🪙 抓取加密貨幣...")
    crypto = fetch_crypto()
    market_data["crypto"] = crypto
    for sym, data in crypto.items():
        print(f"  {sym}: {data['emoji']} {data['price']} ({data['pct']})")

    # 3. 新聞
    print("\n📰 抓取新聞（RSS）...")
    tw_news     = fetch_all_news("tw",     session_info)
    us_news     = fetch_all_news("us",     session_info)
    global_news = fetch_all_news("global", session_info)
    print(f"  台股：{len(tw_news)} 篇 | 美股：{len(us_news)} 篇 | 國際：{len(global_news)} 篇")

    # 3b. 金十數據（國際即時快訊 + 行事曆）
    jin10_flash_items    = []
    jin10_calendar_items = []
    if JIN10_TOKEN:
        print("\n📡 抓取金十數據...")
        jin10_initialize()
        jin10_flash_items    = fetch_jin10_flash(session_info)
        jin10_calendar_items = fetch_jin10_calendar()
        print(f"  快訊：{len(jin10_flash_items)} 則 | 行事曆：{len(jin10_calendar_items)} 個")
    else:
        print("\n⚠️  未設定 JIN10_TOKEN，跳過金十數據")

    # 4. Claude 分析
    print("\n🤖 Claude AI 分析中...")
    print("  分析台股...")
    tw_analysis     = analyze_tw_news(tw_news,     session_info, market_data)
    time.sleep(1)
    print("  分析美股...")
    us_analysis     = analyze_us_news(us_news,     session_info, market_data)
    time.sleep(1)
    print("  分析國際...")
    jin10_prompt_text = build_jin10_text(jin10_flash_items, jin10_calendar_items)
    global_analysis   = analyze_global_news(global_news, session_info, market_data, jin10_prompt_text)
    time.sleep(1)
    print("  分析金十數據...")
    jin10_analysis = analyze_jin10(jin10_flash_items, jin10_calendar_items, session_info) if JIN10_TOKEN else ""

    # 5. 組合資料
    weekend_banner = "（週末版）" if is_weekend() else ""
    session_tag    = f"{session_info['emoji']} {session_info['label']} {weekend_banner}| {session_info['period']}"
    ts             = now_str()
    market_table   = build_market_table(market_data)
    crypto_text    = "\n".join([
        f"{d['emoji']} **{sym}**：{d['price']} ({d['pct']})"
        for sym, d in crypto.items()
    ]) if crypto else "暫無數據"
    tw_links     = build_news_links(tw_news)
    us_links     = build_news_links(us_news)
    global_links = build_news_links(global_news)

    stale_footer = "｜⚠️ 週末數據為上次交易日" if is_weekend() else ""

    # 6. 發送 Discord — 台股
    print("\n📤 發送到 Discord...")
    send_embed(DISCORD_TW, {
        "title":       f"🇹🇼 台股{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color":       0x2ECC71,
        "fields":      [{"name": "📊 大盤指數", "value": truncate(market_table), "inline": False}],
        "footer":      {"text": f"資料來源：Yahoo Finance{stale_footer}"},
    })
    time.sleep(1.2)
    send_discord_message(DISCORD_TW, f"## 🤖 Claude AI 台股分析 — {session_tag}\n\n" + tw_analysis)
    time.sleep(1.2)
    send_embed(DISCORD_TW, {
        "title":  f"📰 台股新聞連結 | {ts}",
        "color":  0x27AE60,
        "fields": [{"name": "🔗 本時段重要新聞", "value": truncate(tw_links), "inline": False}],
        "footer": {"text": "資料來源：Google News / 鉅亨網 / MoneyDJ"},
    })
    time.sleep(1.5)

    # 發送 Discord — 美股
    us_market_text = "\n".join([
        f"{market_data['dji']['emoji']}  **道瓊**：{market_data['dji']['price']} ({market_data['dji']['pct']})" + (" ⚠️未更新" if market_data['dji'].get('stale') else ""),
        f"{market_data['ixic']['emoji']} **納斯達克**：{market_data['ixic']['price']} ({market_data['ixic']['pct']})" + (" ⚠️未更新" if market_data['ixic'].get('stale') else ""),
        f"{market_data['gspc']['emoji']} **S&P 500**：{market_data['gspc']['price']} ({market_data['gspc']['pct']})" + (" ⚠️未更新" if market_data['gspc'].get('stale') else ""),
    ])
    send_embed(DISCORD_US, {
        "title":       f"🇺🇸 美股{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color":       0x3498DB,
        "fields":      [{"name": "📊 三大指數", "value": truncate(us_market_text), "inline": False}],
        "footer":      {"text": f"資料來源：Yahoo Finance{stale_footer}"},
    })
    time.sleep(1.2)
    send_discord_message(DISCORD_US, f"## 🤖 Claude AI 美股分析 — {session_tag}\n\n" + us_analysis)
    time.sleep(1.2)
    send_embed(DISCORD_US, {
        "title":  f"📰 美股新聞連結 | {ts}",
        "color":  0x2980B9,
        "fields": [{"name": "🔗 本時段重要新聞", "value": truncate(us_links), "inline": False}],
        "footer": {"text": "資料來源：Yahoo Finance / MarketWatch / Google News"},
    })
    time.sleep(1.5)

    # 發送 Discord — 國際
    global_market_text = "\n".join([
        f"{market_data['vix']['emoji']}   **VIX**：{market_data['vix']['price']} ({market_data['vix']['pct']})" + (" ⚠️未更新" if market_data['vix'].get('stale') else ""),
        f"{market_data['us10y']['emoji']} **美債10Y**：{market_data['us10y']['price']} ({market_data['us10y']['pct']})" + (" ⚠️未更新" if market_data['us10y'].get('stale') else ""),
        f"{market_data['dxy']['emoji']}   **美元指數**：{market_data['dxy']['price']} ({market_data['dxy']['pct']})" + (" ⚠️未更新" if market_data['dxy'].get('stale') else ""),
        f"{market_data['gold']['emoji']}  **黃金**：{market_data['gold']['price']} ({market_data['gold']['pct']})" + (" ⚠️未更新" if market_data['gold'].get('stale') else ""),
        f"{market_data['oil']['emoji']}   **原油(WTI)**：{market_data['oil']['price']} ({market_data['oil']['pct']})" + (" ⚠️未更新" if market_data['oil'].get('stale') else ""),
    ])
    global_fields = [
        {"name": "📉 總體指標", "value": truncate(global_market_text), "inline": True},
        {"name": "🪙 加密貨幣", "value": truncate(crypto_text),        "inline": True},
    ]
    send_embed(DISCORD_GLOBAL, {
        "title":       f"🌍 國際市場{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color":       0x9B59B6,
        "fields":      global_fields,
        "footer":      {"text": f"資料來源：Yahoo Finance / CoinGecko / 金十數據{stale_footer}"},
    })
    time.sleep(1.2)
    send_discord_message(DISCORD_GLOBAL, f"## 🤖 Claude AI 國際分析 — {session_tag}\n\n" + global_analysis)
    time.sleep(1.2)
    send_embed(DISCORD_GLOBAL, {
        "title":  f"📰 國際新聞連結 | {ts}",
        "color":  0x8E44AD,
        "fields": [{"name": "🔗 本時段重要新聞", "value": truncate(global_links), "inline": False}],
        "footer": {"text": "資料來源：Reuters / BBC / Google News / 金十數據"},
    })

    # 金十數據獨立發送（繁體中文大字體）
    if JIN10_TOKEN and (jin10_flash_items or jin10_calendar_items):
        time.sleep(1.2)
        jin10_msg = build_jin10_discord_message(jin10_flash_items, jin10_calendar_items)
        if jin10_msg:
            send_discord_message(DISCORD_GLOBAL, jin10_msg)
            time.sleep(1.2)
        if jin10_analysis:
            send_discord_message(DISCORD_GLOBAL, f"## 🤖 Claude AI 金十數據分析 — {session_tag}\n\n{jin10_analysis}")

    # 7. 自選股（只在 22:00 盤後執行）
    if session_info["label"] == "盤後晚報":
        time.sleep(2)
        run_watchlist_report()

    # 記錄本次執行成功（防重複用）
    run_state = load_run_state()
    run_state.setdefault(today, [])
    if session_info["label"] not in run_state[today]:
        run_state[today].append(session_info["label"])
    save_run_state(run_state)

    print("\n" + "=" * 65)
    print(f"✅ 日報完成！— {now_str()}")
    print("=" * 65)

# ─────────────────────────────────────────────────────────────
# 排程（每天 08:00 / 15:00 / 22:00 台灣時間）
# ─────────────────────────────────────────────────────────────

def run_schedule():
    print("=" * 65)
    print("⏰ 排程模式啟動")
    print("  每日 08:00 / 15:00 / 22:00（台灣時間）自動執行")
    print("  週六日照常執行，指標數據標註「未更新」")
    print("  按 Ctrl+C 停止")
    print("=" * 65)

    schedule.every().day.at("08:00").do(run_report)
    schedule.every().day.at("15:00").do(run_report)
    schedule.every().day.at("22:00").do(run_report)

    print(f"⏭️  下次執行：{schedule.next_run()}")
    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("⚠️  警告：未設定 ANTHROPIC_API_KEY 環境變數！")
        print("   AI 分析功能將無法使用。\n")

    if "--schedule" in sys.argv:
        run_schedule()
    elif "--watchlist" in sys.argv:
        print("📋 自選股分析模式\n")
        run_watchlist_report()
    else:
        print("💡 提示：加上 --schedule 啟動排程 / --watchlist 只跑自選股\n")
        run_report()
