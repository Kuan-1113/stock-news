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
import pytz
import io

# ─────────────────────────────────────────────────────────────
# 台灣標準時間（NTP stdtime.gov.tw → HTTP → 系統備用）
# ─────────────────────────────────────────────────────────────

def _get_tw_standard_time() -> datetime.datetime:
    """
    向台灣國家標準時間伺服器對時（time.stdtime.gov.tw）。
    失敗時依序降級：worldtimeapi.org → 系統時間。
    只在 run_report() / main() 啟動時呼叫一次。
    """
    TW = pytz.timezone("Asia/Taipei")

    # 1. 優先：台灣 NTP 伺服器（最準確）
    try:
        import ntplib as _ntplib
        c    = _ntplib.NTPClient()
        resp = c.request('time.stdtime.gov.tw', version=3, timeout=5)
        t    = datetime.datetime.fromtimestamp(resp.tx_time, tz=TW)
        print(f"  ⏰ 台灣標準時間（NTP stdtime.gov.tw）：{t.strftime('%Y-%m-%d %H:%M:%S')}")
        return t
    except Exception as e:
        print(f"  ⚠️  NTP 失敗（{e}）→ 嘗試 HTTP 備援")

    # 2. 備援：worldtimeapi.org
    try:
        r = requests.get(
            "https://worldtimeapi.org/api/timezone/Asia/Taipei",
            timeout=5, headers={"User-Agent": "stock-report-bot/1.0"})
        if r.status_code == 200:
            t = datetime.datetime.fromisoformat(r.json()["datetime"])
            print(f"  ⏰ 台灣時間（worldtimeapi.org）：{t.strftime('%Y-%m-%d %H:%M:%S')}")
            return t
    except Exception as e2:
        print(f"  ⚠️  HTTP 備援失敗（{e2}）→ 使用系統時間")

    # 3. 最終備用：系統時間（pytz TW_TZ）
    t = datetime.datetime.now(TW)
    print(f"  ⏰ 系統時間（pytz 備用）：{t.strftime('%Y-%m-%d %H:%M:%S')}")
    return t

try:
    import zhconv
    def _s2tw(text: str) -> str:
        return zhconv.convert(text, "zh-tw")
except ImportError:
    def _s2tw(text: str) -> str:
        return text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from technical_indicators import (get_full_indicators, format_indicators_for_prompt,
                                  get_data_quality_report, classify_signals, format_signal_tiers)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
JIN10_TOKEN       = os.environ.get("JIN10_TOKEN", "")
REPORT_SESSION    = os.environ.get("REPORT_SESSION", "auto")   # morning / afternoon / evening / auto
FORCE_RUN         = os.environ.get("FORCE_RUN", "false").lower() == "true"
RUN_STATE_FILE    = "run_state.json"
# Claude 模型名稱（可透過環境變數覆寫，避免 Anthropic 改版時需動程式碼）
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL",      "claude-sonnet-4-6")
# 輕量任務（選股決策、金十摘要）使用 Haiku，費用約 1/5
CLAUDE_MINI_MODEL = os.environ.get("CLAUDE_MINI_MODEL", "claude-haiku-4-5-20251001")

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
# AI 動態選股候選池 — 每次執行時從 00981A 當日成分股動態抓取
# ─────────────────────────────────────────────────────────────

def _load_candidate_stocks() -> list[dict]:
    """
    從 00981A 野村台灣趨勢動能 ETF 取得當日成分股作為候選池。
    此 ETF 為主動型，每個交易日更新持倉，能反映當天市場趨勢。
    失敗時回傳備用靜態清單。
    """
    import sys, os
    # 讓 stock_daily.py 可以 import strategy_agent（跨目錄）
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        from strategy_agent.etf_universe import fetch_00981A_stocks
        stocks = fetch_00981A_stocks()
        if stocks:
            # 轉成 {"symbol": "XXXX.TW", "name": "股票名稱"} 格式
            result = [{"symbol": s["symbol"], "name": s["name"]} for s in stocks]
            print(f"  ✅ 候選池：00981A 當日成分股 {len(result)} 支")
            return result
    except Exception as e:
        print(f"  ⚠️ 動態抓取 00981A 失敗：{e}，改用靜態備用清單")

    # 備用靜態清單（00981A 常見成分股）
    return [
        {"symbol": "2330.TW", "name": "台積電"},
        {"symbol": "2454.TW", "name": "聯發科"},
        {"symbol": "2308.TW", "name": "台達電"},
        {"symbol": "2382.TW", "name": "廣達"},
        {"symbol": "6669.TW", "name": "緯穎"},
        {"symbol": "2317.TW", "name": "鴻海"},
        {"symbol": "3711.TW", "name": "日月光投控"},
        {"symbol": "2379.TW", "name": "瑞昱"},
        {"symbol": "3034.TW", "name": "聯詠"},
        {"symbol": "2303.TW", "name": "聯電"},
        {"symbol": "2327.TW", "name": "國巨"},
        {"symbol": "2357.TW", "name": "華碩"},
        {"symbol": "6415.TW", "name": "矽力-KY"},
        {"symbol": "2376.TW", "name": "技嘉"},
        {"symbol": "2395.TW", "name": "研華"},
    ]


CANDIDATE_STOCKS: list[dict] = []  # 執行時由 run_watchlist_report() 動態載入

# ─────────────────────────────────────────────────────────────
# 時段判斷
# ─────────────────────────────────────────────────────────────

def get_session_info() -> dict:
    now   = datetime.datetime.now(TW_TZ)
    today = now.strftime("%m/%d")
    prev  = (now - datetime.timedelta(days=1)).strftime("%m/%d")

    _sessions = {
        "morning":   {"label": "盤前早報", "period": f"{prev} 22:00 ～ {today} 08:00", "emoji": "🌅", "start_h": 22, "end_h": 8},
        "afternoon": {"label": "盤中午報", "period": f"{today} 08:00 ～ {today} 15:00", "emoji": "☀️", "start_h": 8,  "end_h": 15},
        "evening":   {"label": "盤後晚報", "period": f"{today} 15:00 ～ {today} 22:00", "emoji": "🌙", "start_h": 15, "end_h": 22},
    }
    # Workflow 明確傳入時段 → 直接使用
    if REPORT_SESSION in _sessions:
        return _sessions[REPORT_SESSION]
    # 自動偵測（手動觸發 / 本機測試）
    h = now.hour
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
# Claude AI — 含餘額不足偵測與 Discord 通知
# ─────────────────────────────────────────────────────────────

# 餘額警告節流：最多每 7 天送一次 Discord 通知，避免天天轟炸
_BALANCE_ALERT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "balance_alert_state.json")
_BALANCE_ALERT_THROTTLE_DAYS = 7

# Anthropic 餘額不足時，回應 body 會含有這些關鍵字（HTTP 400 或 402）
_BALANCE_KEYWORDS = ("credit", "balance", "billing", "payment", "insufficient")


def _load_balance_state() -> dict:
    try:
        with open(_BALANCE_ALERT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_balance_state(state: dict):
    try:
        with open(_BALANCE_ALERT_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 無法寫入 balance_alert_state.json：{e}")


def _notify_balance_low():
    """
    偵測到 API 餘額不足時，發送 Discord 通知。
    每 {_BALANCE_ALERT_THROTTLE_DAYS} 天最多通知一次，不天天騷擾。
    """
    state   = _load_balance_state()
    today   = datetime.datetime.now(TAIPEI_TZ).date()
    last_dt = state.get("last_alert_date")

    if last_dt:
        last_date = datetime.date.fromisoformat(last_dt)
        if (today - last_date).days < _BALANCE_ALERT_THROTTLE_DAYS:
            print(f"⏭️ 餘額通知已在 {_BALANCE_ALERT_THROTTLE_DAYS} 天內發送過（{last_dt}），跳過")
            return

    msg = (
        "🚨 **Claude API 餘額不足 — 報告分析功能已中斷**\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"偵測時間：{datetime.datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M')}\n\n"
        "📋 **影響範圍**\n"
        "• 台股／美股／國際日報的 AI 分析段落將顯示空白\n"
        "• 自選股個股深度分析無法產生\n\n"
        "💳 **解決方式**\n"
        "請前往 https://console.anthropic.com/settings/billing 儲值\n\n"
        "⏰ 此通知每 7 天最多發送一次，不會天天打擾你。"
    )

    sent = False
    for url in [DISCORD_TW, DISCORD_US, DISCORD_GLOBAL, DISCORD_WATCHLIST]:
        if url:
            try:
                r = requests.post(url, json={"content": msg}, timeout=10)
                if r.status_code in (200, 204):
                    sent = True
                    print(f"✅ 餘額不足通知已發送至 Discord")
                    break   # 只需送出一次即可
            except Exception as e:
                print(f"⚠️ 發送餘額通知失敗：{e}")

    # 無論是否送成功，都更新狀態（避免連線問題造成重複嘗試）
    state["last_alert_date"] = today.isoformat()
    _save_balance_state(state)


def claude_call(prompt: str, max_tokens: int = 1200, model: str = None) -> str:
    """
    model=None        → 使用 CLAUDE_MODEL（Sonnet，深度分析）
    model=CLAUDE_MINI → 使用 CLAUDE_MINI_MODEL（Haiku，簡單任務）

    餘額不足（HTTP 400/402）→ 回傳明確提示 + 觸發 Discord 通知（7 天節流）
    """
    use_model = model or CLAUDE_MODEL
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": use_model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()

        # ── 錯誤處理 ──────────────────────────────────
        err_body = r.text.lower()
        print(f"❌ Claude API 錯誤：HTTP {r.status_code} {r.text[:300]}")

        # 餘額不足（HTTP 400 / 402）
        if r.status_code in (400, 402) and any(kw in err_body for kw in _BALANCE_KEYWORDS):
            _notify_balance_low()
            return "🚨 Claude API 餘額不足，AI 分析暫停。請至 console.anthropic.com 儲值。"

        # 速率限制
        if r.status_code == 429:
            return f"⏳ Claude API 請求過於頻繁（rate limit），請稍後再試。"

        # 其他錯誤
        return f"⚠️ AI 分析暫時無法使用（HTTP {r.status_code}）"

    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"⚠️ AI 分析暫時無法使用（連線異常：{str(e)[:80]}）"

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
            emoji = "🔴" if pct >= 0 else "🟢"   # 台灣慣例：紅=漲，綠=跌
            vols  = [v for v in volumes if v is not None]
            ma5   = round(sum(closes[-5:])  / 5,  2) if len(closes) >= 5  else None
            ma10  = round(sum(closes[-10:]) / 10, 2) if len(closes) >= 10 else None
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
                emoji = "🔴" if pct >= 0 else "🟢"   # 台灣慣例：紅=漲，綠=跌
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

        # 擴大時間範圍為最近 24 小時，不局限在當日時段，確保重要事件不遺漏
        now      = now_tw()
        cutoff   = now - datetime.timedelta(hours=24)

        result = []
        for item in items:
            content  = _s2tw(item.get("content") or "")
            time_raw = item.get("time") or ""
            if not content:
                continue
            try:
                pub_dt = datetime.datetime.fromisoformat(
                    time_raw.replace("Z", "+00:00")).astimezone(TW_TZ)
            except Exception:
                pub_dt = None
            # 只過濾超過 24 小時的舊訊息
            if pub_dt and pub_dt < cutoff:
                continue
            result.append({
                "time":  pub_dt.strftime("%m/%d %H:%M") if pub_dt else "",
                "title": content[:150],
                "pub_dt": pub_dt,
            })

        # 依時間新到舊排序，取最近 25 則
        result.sort(key=lambda x: x.get("pub_dt") or datetime.datetime.min.replace(tzinfo=TW_TZ), reverse=True)
        result = [{k: v for k, v in r.items() if k != "pub_dt"} for r in result[:25]]
        print(f"  金十快訊：取得 {len(result)} 則（近24小時）")
        return result
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

# ─────────────────────────────────────────────────────────────
# 類股 / 板塊強弱數據
# ─────────────────────────────────────────────────────────────

# 各族群前5大龍頭股（供領漲族群使用）
SECTOR_LEADERS = {
    "半導體":       ["台積電(2330)", "聯電(2303)", "日月光投控(3711)", "南電(8046)", "矽力-KY(6415)"],
    "電子/AI供應鏈": ["鴻海(2317)", "廣達(2382)", "緯創(3231)", "英業達(2356)", "仁寶(2324)"],
    "IC設計":       ["聯發科(2454)", "聯詠(3034)", "瑞昱(2379)", "世芯-KY(6598)", "力旺(6289)"],
    "AI伺服器":     ["廣達(2382)", "緯穎(6669)", "英業達(2356)", "技嘉(2376)", "微星(2377)"],
    "金融":         ["富邦金(2881)", "國泰金(2882)", "中信金(2891)", "元大金(2885)", "玉山金(2884)"],
    "航運":         ["長榮(2603)", "陽明(2609)", "萬海(2615)", "台驊(2636)", "中航(2612)"],
    "傳產/石化":    ["台塑(1301)", "南亞(1303)", "台化(1326)", "台塑化(6505)", "亞泥(1102)"],
}


def fetch_tw_sectors() -> list:
    """抓取台股各類股代表股漲跌幅，依漲跌排序"""
    TW_SECTOR_STOCKS = [
        ("2330.TW", "半導體"),
        ("2317.TW", "電子/AI供應鏈"),
        ("2454.TW", "IC設計"),
        ("2382.TW", "AI伺服器"),
        ("2882.TW", "金融"),
        ("2609.TW", "航運"),
        ("1301.TW", "傳產/石化"),
    ]
    result = []
    for sym, name in TW_SECTOR_STOCKS:
        q = fetch_yahoo(sym, name)
        if q.get("price") != "N/A":
            pct_str = q.get("pct", "0%")
            try:
                pct_f = float(pct_str.replace("%", "").replace("+", ""))
            except Exception:
                pct_f = 0.0
            result.append({
                "name": name, "symbol": sym,
                "pct": pct_str, "pct_f": pct_f,
                "emoji": q.get("emoji", "⚪"),
            })
        time.sleep(0.15)
    return sorted(result, key=lambda x: x["pct_f"], reverse=True)


def fetch_us_sectors() -> list:
    """抓取美股各板塊 ETF (SPDR) 漲跌幅，依漲跌排序"""
    US_SECTOR_ETFS = [
        ("XLK",  "科技"),
        ("XLC",  "通訊服務"),
        ("XLE",  "能源"),
        ("XLF",  "金融"),
        ("XLV",  "醫療保健"),
        ("XLI",  "工業"),
        ("XLY",  "非必需消費"),
        ("XLP",  "必需消費"),
        ("XLB",  "材料"),
        ("XLRE", "房地產"),
    ]
    result = []
    for etf, name in US_SECTOR_ETFS:
        q = fetch_yahoo(etf, name)
        if q.get("price") != "N/A":
            pct_str = q.get("pct", "0%")
            try:
                pct_f = float(pct_str.replace("%", "").replace("+", ""))
            except Exception:
                pct_f = 0.0
            result.append({
                "name": name, "symbol": etf,
                "pct": pct_str, "pct_f": pct_f,
                "emoji": q.get("emoji", "⚪"),
            })
        time.sleep(0.15)
    return sorted(result, key=lambda x: x["pct_f"], reverse=True)


def format_sectors_text(sectors: list) -> str:
    """排序後格式化：前3強 + 後2弱，節省 prompt token"""
    if not sectors:
        return "（數據未取得）"
    top    = sectors[:3]
    bottom = sectors[-2:] if len(sectors) > 3 else []
    rows   = top + (["---"] if bottom else []) + bottom
    lines  = []
    for s in rows:
        if s == "---":
            lines.append("  …")
        else:
            lines.append(f"  {s['emoji']} {s['name']}：{s['pct']}")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# AI & AI Agent 全球新聞
# ─────────────────────────────────────────────────────────────

RSS_AI_FEEDS = [
    "https://news.google.com/rss/search?q=AI+artificial+intelligence+AGI+breakthrough&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=OpenAI+Anthropic+Google+AI+agent&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=AI+robot+LLM+invention+2025&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.arstechnica.com/arstechnica/index",
]

def fetch_ai_news(session_info: dict) -> list:
    """抓取 AI & AI Agent 全球最新動態新聞"""
    all_articles, seen_titles = [], set()
    for url in RSS_AI_FEEDS:
        for a in fetch_rss(url, limit=10):
            key = re.sub(r"\s+", "", a["title"].lower())[:30]
            if key not in seen_titles:
                seen_titles.add(key)
                all_articles.append(a)
        time.sleep(0.3)
    ai_keywords = [
        "ai", "artificial intelligence", "llm", "gpt", "claude", "gemini", "agent",
        "robot", "openai", "deepmind", "anthropic", "chatgpt", "copilot",
        "inference", "foundation model", "generative", "nvidia", "automation",
    ]
    relevant = [a for a in all_articles if any(kw in a["title"].lower() for kw in ai_keywords)]
    other    = [a for a in all_articles if a not in relevant]
    result   = (relevant + other)
    result.sort(key=lambda x: x["pub_dt"] or datetime.datetime.min.replace(tzinfo=TW_TZ), reverse=True)
    print(f"  AI新聞：取得 {len(result)} 篇（AI相關 {len(relevant)} 篇）")
    return result[:12]


def fetch_vip_news() -> dict:
    """抓取馬斯克 / 川普 / 黃仁勳 最新動態"""
    configs = [
        ("musk",   "https://news.google.com/rss/search?q=Elon+Musk+Tesla+SpaceX+X&hl=en-US&gl=US&ceid=US:en"),
        ("trump",  "https://news.google.com/rss/search?q=Donald+Trump+tariff+economy+market&hl=en-US&gl=US&ceid=US:en"),
        ("jensen", "https://news.google.com/rss/search?q=Jensen+Huang+NVIDIA+AI+chip&hl=en-US&gl=US&ceid=US:en"),
    ]
    result = {}
    for key, url in configs:
        articles = fetch_rss(url, limit=5)
        result[key] = articles[:3]
        time.sleep(0.25)
    return result


def analyze_ai_news(articles: list, session_info: dict) -> str:
    """AI & AI Agent 全球動態分析"""
    if not articles:
        return ""
    prompt = f"""AI科技趨勢分析師。{now_str()}，「{session_info['label']}」。
【AI全球動態】
{build_news_text(articles)}

以繁體中文寫（700字內），必帶具體公司/產品名稱，禁泛說「AI持續進步」，禁用 Markdown 表格，全程 bullet（•）。
每個論點格式：「具體事件（公司/產品）→ 技術/商業意義 → 對產業鏈影響 → 台股受惠/受壓方向」

**🚀 重大突破/新發布**（1-3個）
• 公司/產品：做了什麼（具體功能/參數）→ 技術意義 → 誰是直接受益者

**⚔️ AI賽局多空論點**
利多方向（AI投資加速的理由＋哪些公司/族群直接受惠）：
•
利空方向（競爭加劇/替代效應/監管風險 → 哪些公司/族群受壓）：
•

**📈 台灣科技股影響路徑**（必須完整輸出，2-3檔）
• 台股代號 公司名：全球AI事件X → 訂單/技術/競爭的傳導路徑 → 股價偏多/偏空/觀望

> ⚠️ AI 生成，不構成投資建議。"""
    return _strip_md_tables(claude_call(prompt, max_tokens=1400))

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
# Phase 1 輔助：新聞資料品質摘要
# ─────────────────────────────────────────────────────────────

def _news_quality(articles: list, session_info: dict) -> str:
    """生成新聞資料品質標記，供 Phase 1 資訊收斂使用"""
    if not articles:
        return "❌ 本時段無新聞資料"
    total  = len(articles)
    timed  = sum(1 for a in articles if a.get("pub_dt"))
    in_win = sum(1 for a in articles
                 if a.get("pub_dt") and
                 a["pub_dt"] >= datetime.datetime.now(TW_TZ) - datetime.timedelta(hours=12))
    latest = max((a["pub_dt"] for a in articles if a.get("pub_dt")),
                 default=None)
    latest_s = latest.strftime("%m/%d %H:%M") if latest else "不明"
    return (
        f"✅ 新聞：共 {total} 則，有時間戳 {timed} 則，12h內 {in_win} 則，最新：{latest_s}"
    )


# ─────────────────────────────────────────────────────────────
# Claude 分析 — 台股 / 美股 / 國際
# ─────────────────────────────────────────────────────────────

def build_news_text(articles: list, limit: int = 8) -> str:
    """新聞條列（僅標題+時間，不帶摘要，節省 input token）"""
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        pub  = a["pub_dt"].strftime("%m/%d %H:%M") if a["pub_dt"] else ""
        lines.append(f"{i}. [{pub}] {a['title']}")
    return "\n".join(lines) if lines else "（本時段暫無新聞）"

def analyze_tw_news(articles, session_info, market_data, tw_sectors_text: str = "", tw_sectors: list = None) -> str:
    twii = market_data.get("twii", {})
    wknd = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""

    # 領漲族群龍頭股（取第一名族群）
    leaders_block = ""
    if tw_sectors:
        top_sector = tw_sectors[0]["name"]
        leaders    = SECTOR_LEADERS.get(top_sector, [])
        if leaders:
            leaders_block = f"\n【領漲第一族群龍頭股】{top_sector} → {'、'.join(leaders)}"

    sector_block = (
        f"\n【台股類股今日強弱（代表股漲跌，由強至弱）】\n{tw_sectors_text}{leaders_block}"
        if tw_sectors_text else ""
    )

    news_quality = _news_quality(articles, session_info)
    prompt = f"""台股分析師。{wknd}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌。
不需按固定章節填寫，以邏輯流暢為主，總字數 1100 字以內。

━━━ PHASE 1：資訊收斂 ━━━
✅ [TWSE] 加權指數：{fmt_quote(twii) if twii else 'N/A'}
✅ [TWSE] 類股：{tw_sectors_text[:100] if tw_sectors_text else '無法取得'}…
{news_quality}

━━━ 台股新聞 ━━━
{build_news_text(articles)}

━━━ PHASE 2 + 3 核心任務（順序自由，篇幅自由調配）━━━

**A. 歸因分析（必做）**
今日台股主要驅動力是什麼？
• 識別 1-3 個主因（指數/族群/消息/外部），說明哪個最主導
• 這個驅動是一日事件還是延續性趨勢？後續是否有跟進空間？
• 若方向不明，列出競爭性解釋並說明觀察哪個指標可確認

**B. 類股輪動與個股（必做）**
• 強勢族群：漲幅+驅動因素+龍頭股代號
• 弱勢族群：跌幅+壓力來源
• 值得追蹤的個股（3-4檔）：代號+事件+評估

**C. 多空決策（必做）**
• 多方核心論點（訊號→邏輯→目標）
• 空方核心論點（訊號→邏輯→風險位）
• 當前偏向 + 最關鍵決定因素
• 操作建議：觸發條件→標的→方向→停損

**D. AI 自由觀察（選做，若有特別洞察）**
今日有什麼值得關注但上述框架未涵蓋的異常或模式？
（跨市場訊號、行為偏誤、歷史類比、量價異常等）"""
    return _strip_md_tables(claude_call(prompt, max_tokens=1600))

def analyze_us_news(articles, session_info, market_data, us_sectors_text: str = "", vip_news: dict = None) -> str:
    dji  = market_data.get("dji",  {})
    ixic = market_data.get("ixic", {})
    gspc = market_data.get("gspc", {})
    wknd = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    sector_block = f"\n【S&P 500 板塊 ETF 今日漲跌（由強至弱）】\n{us_sectors_text}" if us_sectors_text else ""

    # 重要人物最新動態
    vip_block = ""
    if vip_news:
        def _titles(key):
            return "　".join(
                f"[{a['pub_dt'].strftime('%H:%M')}] {a['title'][:50]}" if a.get("pub_dt") else a["title"][:50]
                for a in (vip_news.get(key) or [])[:2]
            ) or "（暫無新聞）"
        vip_block = (
            "\n【重要人物最新動態】\n"
            f"🚀 馬斯克（Musk）：{_titles('musk')}\n"
            f"🏛️ 川普（Trump）：{_titles('trump')}\n"
            f"🟩 黃仁勳（Jensen Huang）：{_titles('jensen')}"
        )

    news_quality = _news_quality(articles, session_info)
    prompt = f"""美股分析師。{wknd}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌。
不需按固定章節填寫，以邏輯流暢為主，總字數 1100 字以內。

━━━ PHASE 1：資訊收斂 ━━━
✅ [Yahoo Finance] 道瓊：{fmt_quote(dji) if dji else 'N/A'} ／ 納指：{fmt_quote(ixic) if ixic else 'N/A'} ／ S&P：{fmt_quote(gspc) if gspc else 'N/A'}
✅ [SPDR ETF] {us_sectors_text[:80] if us_sectors_text else '板塊資料無法取得'}…
{news_quality}
{vip_block}

━━━ 美股新聞 ━━━
{build_news_text(articles)}

━━━ PHASE 2 + 3 核心任務（順序自由，篇幅自由調配）━━━

**A. 歸因分析（必做）**
今日美股主要驅動力是什麼？
• 識別 1-3 個主因（總經數據/Fed聲明/財報/板塊輪動/地緣政治），說明相對重要性
• 是單次事件還是持續趨勢？後續哪個數據或事件會確認或推翻？
• 各因素對不同板塊/個股的差異化影響

**B. 板塊與個股（必做）**
• 強勢板塊（ETF數值）：驅動因素+可關注個股
• 弱勢板塊（ETF數值）：壓力來源+迴避方向
• 重點個股（3-4檔）：代號+事件+評估

**C. 多空決策（必做）**
• 多方核心論點（訊號→邏輯→目標位）
• 空方核心論點（訊號→邏輯→下行風險）
• 當前偏向 + 最關鍵決定因素
• 操作建議：觸發條件→標的→方向→停損

**D. 重要人物動態**（有相關新聞才寫）
• 人物：發言/動作 → 受影響板塊/資產

**E. AI 自由觀察**（選做）
有什麼值得關注的異常或框架外洞見？（跨資產訊號、歷史類比、行為偏誤等）"""
    return _strip_md_tables(claude_call(prompt, max_tokens=1700))

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
    news_quality = _news_quality(articles, session_info)
    prompt = f"""國際財經分析師，採用三階段迭代分析框架。{wknd}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
每個論點格式：「事件/數據（數值）→ 傳導機制 → 影響方向」。禁空話，禁 Markdown 表格，全程 bullet（•）。台灣慣例：🔴=漲/利多，🟢=跌/利空。

━━━ PHASE 1：資訊收斂確認 ━━━
✅ [Yahoo Finance] VIX：{fmt_quote(vix) if vix else 'N/A'} ／ 美債10Y：{fmt_quote(us10y) if us10y else 'N/A'} ／ DXY：{fmt_quote(dxy) if dxy else 'N/A'}
✅ [Yahoo Finance] 黃金：{fmt_quote(gold) if gold else 'N/A'} ／ 原油：{fmt_quote(oil) if oil else 'N/A'}
✅ [CoinGecko] BTC：{btc.get('price','N/A')}({btc.get('pct','')}) ／ ETH：{eth.get('price','N/A')}({eth.get('pct','')})
{news_quality}
{'✅ [金十數據] 快訊已附入' if jin10_text else '⚠️ 金十數據：未取得'}

━━━ 國際新聞 ━━━
{build_news_text(articles)}
{jin10_text if jin10_text else ''}

━━━ PHASE 2：深度研讀（900字以內）━━━

━━━ PHASE 2 + 3 核心任務（順序自由，篇幅自由調配）━━━

**A. 全球歸因分析（必做）**
全球市場今日的宏觀主軸是什麼？
• VIX/美債/DXY 數值說明什麼？Risk-On 還是 Risk-Off？為什麼？
• 識別 1-3 個最主要驅動事件，說明傳導路徑
• 若指標之間互相矛盾（如 VIX 下降但美債上升），以哪個為主要訊號？理由？

**B. 重大事件影響（必做）**
• 事件（數值）→ 傳導路徑 → 🔴利多/🟢利空（高/中/低）

**C. 大宗商品與加密**
• 品項：漲跌 → 驅動因素 → 對通膨/央行政策的隱含訊號

**D. 多空決策（必做）**
• 多方力量（1-2個支撐因素）→ 受惠資產/族群
• 空方風險（1-2個壓制因素）→ 受壓資產/族群
• 當前 Risk-On/Risk-Off 偏向 + 最關鍵決定因子
• 對台股的具體傳導路徑：哪些族群（代號）受惠或受壓，為什麼？

**E. AI 自由觀察**（選做）
有什麼指標組合、跨市場訊號、或歷史類比值得關注？

**F. 本週重要財經事件**（若有）
• 數據名稱：預期值 → 若超預期/不如預期，影響方向"""
    return _strip_md_tables(claude_call(prompt, max_tokens=1600))

def analyze_jin10(flash: list, calendar: list, session_info: dict) -> str:
    """針對金十數據進行專屬 AI 分析（近24小時重點事件整理）"""
    if not flash and not calendar:
        return ""
    jin10_text = build_jin10_text(flash, calendar)
    prompt = f"""國際財經分析師。根據以下金十數據近24小時快訊與行事曆，以繁體中文撰寫完整分析（700字內）。
嚴禁表格，全程 bullet（• 開頭）。台灣慣例：🔴=漲/利多，🟢=跌/利空。
每個論點格式：「具體事件/數值 → 傳導機制 → 影響方向與程度」

{jin10_text}

以下四節全部完整輸出：

**🔥 最重大事件因果分析**（3-5個）
• 事件（數值）→ 影響資產 → 🔴利多/🟢利空 → 市場已反應或尚未反應

**⚔️ 全球多空格局**
多方力量：（數據/事件支撐上漲的2個最強理由）
空方風險：（數據/事件可能壓制市場的2個最大威脅）
當前偏向：[多/空/均衡] → 最關鍵因素一句

**🇹🇼 台股/亞股影響路徑**
• 全球因素X → 傳導到台灣的路徑 → 受惠族群（代號）或受壓族群
（每點說明因果，不要只說「台股受惠」而不說為什麼）

**📅 重要財經數據**
• 數據名稱：預期值 → 若高於/低於預期，影響方向（升/降/多/空）"""
    return _strip_md_tables(claude_call(prompt, max_tokens=1000))

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

def _strip_md_tables(text: str) -> str:
    """
    後處理：把 AI 偶爾生成的 Markdown 表格行轉為 bullet 文字，
    避免 Discord 顯示殘缺表格或只有表頭的情況。
    """
    lines  = text.split("\n")
    output = []
    for line in lines:
        s = line.strip()
        # 表格分隔行（|---|---| 格式）直接跳過
        if s.startswith("|") and s.endswith("|") and re.match(r"^\|[-:\s|]+\|$", s):
            continue
        # 內容行（| A | B | C | 格式）→ 轉 bullet
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells:
                output.append("• " + "　".join(cells))
            continue
        output.append(line)
    return "\n".join(output)


def analyze_single_stock(symbol: str, name: str, quote: dict, news_ctx: str = "") -> str:
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 3:
        price_history = "近3日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-3:]])

    stale_note = "⚠️ 注意：今日為週末，以下為上次交易日收盤數據。\n" if quote.get("stale") else ""

    # 取得完整技術指標
    print(f"    計算技術指標 {symbol}...")
    ind = get_full_indicators(symbol)
    indicators_text = format_indicators_for_prompt(ind)
    quality_report  = get_data_quality_report(ind)

    # ── Phase 1 訊號分級（程式層客觀分類）──────────────────────
    news_total    = len([l for l in (news_ctx or "").split("\n") if l.strip()])
    news_recent   = 0   # stock_daily 無法在此得知時效，保守設 0（已在 fetch_all_news 過濾）
    tier_result   = classify_signals(ind, news_count=news_total, news_recent_12h=news_recent)
    tier_report   = format_signal_tiers(tier_result)

    t1_count = sum(1 for v in tier_result['classifications'].values() if v == 'tier1')

    prompt = f"""你是資深股票分析師。以下提供程式預處理後的結構化資料，請用你的分析判斷力進行深度研判。
禁空話，禁 Markdown 表格，全程 bullet（•）。總字數 1800 字以內。{stale_note}

━━━ 標的 ━━━
{name}（{symbol}）現價 {quote.get('price','N/A')} {quote.get('currency','')} 漲跌 {quote.get('change','N/A')}（{quote.get('pct','N/A')}）{' ' + price_history if price_history else ''}

━━━ PHASE 1：資訊收斂（程式預處理結果）━━━

【資料來源品質確認】
{quality_report}

【訊號分級（客觀規則自動評估）】
{tier_report}
※ Tier 1 = 主要訊號（分析錨點）　Tier 2 = 輔助確認　Tier 3 = 背景雜訊（避免過度解讀）

━━━ 原始數據 ━━━
{indicators_text}

━━━ 消息面 ━━━
{news_ctx if news_ctx else '（暫無新聞）'}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
以下五個章節為【必填】，每節至少 2-3 bullet，數值務必帶括號標注來源（技術/籌碼/基本面/消息）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## ▌近期走勢解讀
• 近 5 日價格行為的核心驅動力（技術突破？籌碼主導？消息催化？估值修正？）— 識別 1-2 個主因及其相對重要性
• 此驅動力屬於「短暫脈衝」（1-5 日消散）還是「持續結構」（趨勢改變）？說明判斷依據
• Tier 1 訊號整合：多個訊號是互相強化還是存在矛盾？整合後的整體方向為何？

## ▌技術面深度分析
• 趨勢結構：目前位於上升 / 下降 / 盤整哪種趨勢？關鍵均線（MA5/MA20/MA60）排列與支撐壓力（帶具體價位）
• 量價型態：成交量配合程度，近期是否出現量能異常（爆量 / 縮量 / 背離）？
• 重要技術位：近一個月最強支撐位與最重壓力位（帶具體價格）、目前距離各自幾 %
• 動能指標解讀：RSI 目前數值與方向、MACD 柱體縮擴、布林帶開口變化，各自傳遞何種訊號

## ▌籌碼面分析
• 三大法人近 5 日動向（若有資料）：外資 / 投信 / 自營商各自買賣超方向與金額
• 若籌碼資料不足，說明替代觀察指標（如：融資融券餘額、大戶持股比例、法人連買/連賣天數）
• 籌碼面整體是否支持目前的價格方向？給出明確判斷

## ▌基本面概況
• 產業定位：在所屬族群中的競爭地位（龍頭 / 跟隨者 / 利基廠），主要護城河
• 近期基本面事件：最新財報重點、法說重點、重大訂單或合作（若消息欄有提及，帶具體數字）
• 估值合理性：若有 P/E、殖利率、P/B 等數據，說明目前是否合理偏高/偏低
• 近期潛在催化劑或下行風險（產業週期、政策、競爭對手動態）

## ▌操作建議
• 多方論點（至少 2 點，帶具體目標價與理由）
• 空方風險（至少 2 點，帶關鍵壓力位 / 停損位與觸發條件）
• 操作框架：進場條件 → 短線目標（帶價位） → 停損（帶價位），預估風報比
• 信心水準（高 / 中 / 低）+ 最大不確定因子（一句話點出最重要的未知變數）
  ※ 本次 Tier 1 訊號共 {t1_count} 個，作為信心評估基礎

> ⚠️ AI 生成，不構成投資建議。"""
    raw = claude_call(prompt, max_tokens=2600)
    return _strip_md_tables(raw)

def ai_pick_watchlist(candidates: list, market_ctx: str) -> list:
    """讓 Claude 從候選股中挑選當日最值得追蹤的 2-3 檔，提供詳細選股理由"""
    cand_list = "\n".join([f"{s['name']}（{s['symbol']}）" for s in candidates])
    prompt = f"""台股選股，{now_str()}。從候選股選2-3檔今日最值得追蹤的個股。
選股準則：①今日有催化劑（法說/財報/訂單/AI題材）②技術關鍵位突破或守穩 ③當日市場主旋律龍頭

【今日大盤行情】
{market_ctx}

【候選股清單】
{cand_list}

輸出格式（每檔一段，嚴格遵守，不加其他文字）：

PICK:代號|股票名稱
理由1: 技術面 — （說明技術訊號，帶具體數字，如：站上20日均線，RSI由32反彈至48）
理由2: 催化劑 — （說明消息面或基本面催化；無重大消息則填「技術主導，無重大消息面」）
理由3: 大盤配合度 — （今日大盤環境是否有利，說明族群聯動或資金流向）
理由4: 目標位 — （短線目標價或上方壓力位，以及停損參考位）
理由5: 風險提示 — （一個最大下行風險，如法規、競爭、總經）

---（每檔之間用三條橫線隔開）---

範例：
PICK:2330.TW|台積電
理由1: 技術面 — 站回1000元整數關卡，5日均線向上穿越20日均線形成黃金交叉，RSI 55仍有上行空間
理由2: 催化劑 — CoWoS封裝需求強勁，法人預估Q3 EPS上修至15元，外資連買3日共10萬張
理由3: 大盤配合度 — 費城半導體指數昨大漲2.3%，AI族群資金回流，台積電ADR溢價1.5%
理由4: 目標位 — 短線目標1050-1080元（前高壓力），跌破980元止損
理由5: 風險提示 — 美中科技管制升溫恐影響先進製程出口許可，需注意地緣政治新聞"""

    response = claude_call(prompt, max_tokens=800, model=CLAUDE_MINI_MODEL)
    picks = []
    current_symbol: str | None = None
    current_candidate: dict | None = None
    current_reasons: list[str] = []

    def _save_current():
        nonlocal current_symbol, current_candidate, current_reasons
        if current_symbol and current_candidate and current_reasons:
            reason_text = "\n".join(current_reasons)
            picks.append({**current_candidate, "ai_reason": reason_text})
        current_symbol = None
        current_candidate = None
        current_reasons = []

    for line in response.split("\n"):
        line = line.strip()
        if not line or line.startswith("---"):
            if current_symbol:
                _save_current()
            continue
        if line.startswith("PICK:"):
            _save_current()
            try:
                parts = line[5:].split("|", 1)
                current_symbol = parts[0].strip()
                current_candidate = next((c for c in candidates if c["symbol"] == current_symbol), None)
                current_reasons = []
            except Exception:
                current_symbol = None
                current_candidate = None
        elif line.startswith("理由") and current_candidate is not None:
            current_reasons.append(line)

    _save_current()  # 儲存最後一檔

    if not picks:  # fallback：AI 解析失敗就取前兩名
        picks = [{**c, "ai_reason": "AI 精選"} for c in candidates[:2]]
    return picks[:3]


def run_watchlist_report():
    """執行自選股日報（每日 22:00）"""
    watchlist = load_watchlist()  # 固定項目（^TWII 加權指數）

    print(f"\n📈 自選股分析 — 固定 {len(watchlist)} 檔 + AI 動態精選...")
    session_info  = get_session_info()
    ts            = now_str()
    weekend_banner = "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    # ── AI 動態選股：每次執行時抓 00981A 當日成分股作為候選池 ──
    print("  🤖 載入 00981A 今日成分股候選池...")
    candidates = _load_candidate_stocks()   # 每日動態，不用靜態清單
    print(f"  🤖 AI 從 {len(candidates)} 支候選股中選股中...")
    market_lines = []
    for s in candidates:
        q = fetch_yahoo(s["symbol"], s["name"])
        if q.get("price") != "N/A":
            market_lines.append(f"{s['name']}（{s['symbol']}）：{fmt_quote(q)}")
        time.sleep(0.15)

    market_ctx = "\n".join(market_lines) if market_lines else "（行情暫時無法取得）"
    ai_picks   = ai_pick_watchlist(candidates, market_ctx)
    print(f"  AI 精選：{', '.join([p['name'] for p in ai_picks])}")

    # 合併清單（固定 + AI 精選，去重）
    fixed_syms = {w["symbol"] for w in watchlist}
    full_list  = list(watchlist) + [p for p in ai_picks if p["symbol"] not in fixed_syms]

    def _fmt_pick_summary(p: dict) -> str:
        reason_lines = [l for l in p.get("ai_reason", "AI 精選").split("\n") if l.strip()]
        header = f"▸ **{p['name']}（{p['symbol']}）**"
        if len(reason_lines) <= 1:
            return f"{header} — {reason_lines[0] if reason_lines else 'AI 精選'}"
        body = "\n".join(f"　{l}" for l in reason_lines)
        return f"{header}\n{body}"

    picks_desc = "\n\n".join([_fmt_pick_summary(p) for p in ai_picks])

    send_embed(DISCORD_WATCHLIST, {
        "title":       f"📊 自選股日報 {session_info['emoji']} {session_info['label']} {weekend_banner}| {ts}",
        "description": (
            f"**時段：** {session_info['period']}\n"
            f"固定追蹤 **{len(watchlist)}** 檔 ＋ AI 精選 **{len(ai_picks)}** 檔\n"
            f"候選池：**00981A 野村台灣趨勢動能 ETF** 當日成分股（{len(candidates)} 支）\n\n"
            f"**🤖 今日 AI 精選理由：**\n{picks_desc}\n\n"
            f"由 Claude AI 生成，不構成投資建議。"
        ),
        "color":  0xF39C12,
        "footer": {"text": "資料來源：TWSE + Yahoo Finance + Google News + Claude AI"},
    })
    time.sleep(1.2)

    ok_count = fail_count = 0
    for stock in full_list:
        symbol = stock["symbol"]
        name   = stock["name"]
        print(f"  分析 {name}（{symbol}）...")
        try:
            quote    = fetch_yahoo(symbol, name)
            print(f"  價格：{fmt_quote(quote)}")
            news_ctx = fetch_stock_news(symbol, name)
            time.sleep(0.5)

            analysis = analyze_single_stock(symbol, name, quote, news_ctx)
            time.sleep(1)

            stale_tag = " ⚠️未更新" if quote.get("stale") else ""
            if stock.get("ai_reason"):
                reason_lines = [l for l in stock["ai_reason"].split("\n") if l.strip()]
                ai_tag = "\n**🤖 AI 精選理由：**\n" + "\n".join(f"> {l}" for l in reason_lines)
            else:
                ai_tag = ""
            send_discord_message(
                DISCORD_WATCHLIST,
                f"## {quote.get('emoji','📊')} **{name}（{symbol}）** — "
                f"{quote.get('price','N/A')} ({quote.get('pct','N/A')}){stale_tag}{ai_tag}\n\n{analysis}"
            )
            ok_count += 1
        except Exception as e:
            fail_count += 1
            print(f"  ❌ {name}（{symbol}）分析失敗，跳過：{e}", flush=True)
            # 仍發一則簡短錯誤訊息，讓用戶知道這支有問題
            try:
                send_discord_message(
                    DISCORD_WATCHLIST,
                    f"⚠️ **{name}（{symbol}）** — 分析暫時失敗（{str(e)[:80]}），已跳過。"
                )
            except Exception:
                pass
        time.sleep(1.5)

    print(f"  ✅ 自選股分析完成（成功 {ok_count} / 失敗 {fail_count}）")

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

    # ── Step 1：取得台灣標準時間（NTP 對時）──────────────────
    print("⏰ 向台灣標準時間伺服器對時...")
    tw_std = _get_tw_standard_time()
    sys_tw = datetime.datetime.now(TW_TZ)
    diff   = abs((tw_std - sys_tw).total_seconds())
    print(f"   系統時間 ：{sys_tw.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   標準時間差：{diff:.1f} 秒{'  ✅ 正常' if diff < 60 else '  ⚠️ 偏差過大，以標準時間為準'}")

    # 優先使用 workflow 傳入的驗證小時（workflow 已對時過）
    # 次選 NTP 結果，最後才用系統時間
    _env_h = os.environ.get("VERIFIED_TW_HOUR", "")
    if _env_h.isdigit():
        verified_h = int(_env_h)
        print(f"   使用 workflow 驗證時間：{verified_h:02d}時")
    else:
        verified_h = tw_std.hour
        print(f"   使用 NTP 驗證時間：{verified_h:02d}時")

    # ── Step 2：時段判斷（以驗證後時間為準）─────────────────
    session_info  = get_session_info()   # 優先用 REPORT_SESSION 環境變數
    today         = tw_std.strftime("%Y-%m-%d")
    run_state     = load_run_state()

    print(f"[INIT] REPORT_SESSION={REPORT_SESSION!r}  →  時段={session_info['label']!r}")
    print(f"[INIT] 台灣標準時間：{tw_std.strftime('%Y-%m-%d %H:%M:%S')}  today={today}")
    print(f"[INIT] run_state 今日已執行：{run_state.get(today, [])}")

    # ── Step 3：時間安全閘（FORCE_RUN 可略過，排程/auto 模式都檢查）──
    # 排程延遲超過 3 小時才會被擋（GitHub cron ±30min 屬正常抖動）
    if not FORCE_RUN:
        _valid_hours = {
            "盤前早報":  set(range(5, 13)),                # 05-12時（預期 08，容忍 ±3h）
            "盤中午報":  set(range(11, 19)),               # 11-18時（預期 15，容忍 ±3h）
            "盤後晚報":  set(range(18, 24)) | {0, 1, 2},  # 18-02時（預期 22，容忍 ±3h）
        }
        valid = _valid_hours.get(session_info["label"], set(range(0, 24)))
        if verified_h not in valid:
            print(f"⏭️  [時間安全閘] 台灣標準時間 {verified_h:02d}:xx 不在"
                  f" {session_info['label']} 有效時窗 {sorted(valid)} 內，略過"
                  f"（GitHub 排程延遲過長或手動執行時段錯誤）")
            print("=" * 65)
            return

    # 防重複：同一時段當天已成功執行過就跳過（FORCE_RUN=true 可略過）
    if not FORCE_RUN and session_info["label"] in run_state.get(today, []):
        print(f"⏭️  {session_info['label']} 今日已執行（{today}），跳過重複執行")
        print("=" * 65)
        return
    if FORCE_RUN:
        print(f"⚡ FORCE_RUN=true，略過防重複檢查")

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

    # 3c. 類股強弱數據
    print("\n📊 抓取類股強弱數據...")
    tw_sectors      = fetch_tw_sectors()
    us_sectors      = fetch_us_sectors()
    tw_sectors_text = format_sectors_text(tw_sectors)
    us_sectors_text = format_sectors_text(us_sectors)
    print(f"  台股類股：{len(tw_sectors)} 個 | 美股板塊：{len(us_sectors)} 個")

    # 3d. AI & AI Agent 全球新聞（早報 08:00 跳過：凌晨 AI 動態稀少，節省 API 費用）
    is_morning = session_info["label"] == "盤前早報"
    ai_news = []
    if not is_morning:
        print("\n🤖 抓取 AI & AI Agent 全球新聞...")
        ai_news = fetch_ai_news(session_info)
    else:
        print("\n⏭️ 早報：跳過 AI 新聞（凌晨動態稀少，節省 token 費用）")

    # 3e. 重要人物動態（美股頻道）
    print("\n👤 抓取重要人物動態（馬斯克/川普/黃仁勳）...")
    vip_news = fetch_vip_news()
    print(f"  Musk:{len(vip_news.get('musk',[]))} Trump:{len(vip_news.get('trump',[]))} Jensen:{len(vip_news.get('jensen',[]))} 則")

    # 4. Claude 分析
    print("\n🤖 Claude AI 分析中...")
    print("  分析台股...")
    tw_analysis     = analyze_tw_news(tw_news,     session_info, market_data, tw_sectors_text, tw_sectors)
    time.sleep(1)
    print("  分析美股...")
    us_analysis     = analyze_us_news(us_news,     session_info, market_data, us_sectors_text, vip_news)
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

    # AI & AI Agent 全球新聞（附在國際頻道）
    if ai_news:
        time.sleep(1.2)
        ai_news_links = build_news_links(ai_news, limit=8)
        send_embed(DISCORD_GLOBAL, {
            "title":  f"🤖 AI & AI Agent 全球動態 | {ts}",
            "color":  0xE74C3C,
            "fields": [{"name": "🔗 最新 AI 新聞", "value": truncate(ai_news_links), "inline": False}],
            "footer": {"text": "資料來源：Google News / Ars Technica"},
        })
        time.sleep(1.2)
        print("  分析 AI & AI Agent 新聞...")
        ai_analysis_result = analyze_ai_news(ai_news, session_info)
        if ai_analysis_result:
            send_discord_message(
                DISCORD_GLOBAL,
                f"## 🤖 Claude AI — AI & AI Agent 全球趨勢 — {session_tag}\n\n{ai_analysis_result}"
            )

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
