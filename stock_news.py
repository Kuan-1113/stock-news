"""
stock_news.py
台股 / 美股 / 國際新聞抓取 + Gemini AI 分析 + Discord 傳送
使用最新 google-genai 2.x SDK (google.genai)

定時規則（台灣時間）：
  08:00 → 整理前天 22:00 ~ 當天 08:00 的新聞
  14:00 → 整理當天 08:00 ~ 14:00 的新聞
  22:00 → 整理當天 14:00 ~ 22:00 的新聞
"""

import os
import sys
import time
import re
import json
import warnings
import datetime
import requests
from urllib.parse import urlparse, urlunparse
from google import genai

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# ── 環境變數 ──────────────────────────────────────────────
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
NEWS_API_KEY     = os.environ.get("NEWS_API_KEY", "")
GNEWS_API_KEY    = os.environ.get("GNEWS_API_KEY", "")
FINNHUB_KEY      = os.environ.get("FINNHUB_KEY", "")
NEWSDATA_API_KEY = os.environ.get("NEWSDATA_API_KEY", "")
JIN10_TOKEN      = os.environ.get("JIN10_TOKEN", "")
DISCORD_TW       = os.environ.get("DISCORD_TW",     "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW")
DISCORD_US       = os.environ.get("DISCORD_US",     "https://discord.com/api/webhooks/1507952945130635307/3Rd1BBhGElvH4N7RZeQaOHNp5FqiCGBO4d9UZwK29dY1wksN70CWYh4MJ19tRfUuSOVX")
DISCORD_GLOBAL   = os.environ.get("DISCORD_GLOBAL", "https://discord.com/api/webhooks/1507953174512668902/QsKOUt5afzwQYfbQQeGi8Tza2-gkLKUJaP-B03lWEyX9C5ops59NuGHLJCK7a8UC9N5-")

TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# ── 時段判斷 ──────────────────────────────────────────────
def get_session_label() -> str:
    """依目前台灣時間決定本次是哪個時段"""
    h = datetime.datetime.now(TW_TZ).hour
    if 8 <= h < 14:
        return "08:00 盤前（前天22:00 ~ 今日08:00）"
    elif 14 <= h < 22:
        return "14:00 盤中（今日08:00 ~ 14:00）"
    else:
        return "22:00 盤後（今日14:00 ~ 22:00）"

def get_session_hours() -> tuple[int, int]:
    """回傳本次時段的起始/結束小時（用於 GNews from/to 參數）"""
    h = datetime.datetime.now(TW_TZ).hour
    if 8 <= h < 14:
        return 22, 8    # 前天22 ~ 今日08
    elif 14 <= h < 22:
        return 8, 14    # 今日08 ~ 14
    else:
        return 14, 22   # 今日14 ~ 22

# ── Gemini 客戶端初始化 ───────────────────────────────────
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini 客戶端初始化成功")
else:
    gemini_client = None
    print("⚠️  未設定 GEMINI_API_KEY，AI 分析將使用備用文字")

# ── 工具函式 ──────────────────────────────────────────────
def now_str() -> str:
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

def clean_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url

def safe_val(text, limit: int = 1000) -> str:
    if not text or str(text).strip() == "":
        return "暫無資料"
    t = str(text).strip()
    return t[:limit] if len(t) > limit else t

# ── Gemini AI 呼叫 ────────────────────────────────────────
def gemini_call(prompt: str, max_tokens: int = 500) -> str:
    if not gemini_client:
        return "AI 分析暫時無法使用（未設定 GEMINI_API_KEY）"
    if not prompt or prompt.strip() == "":
        return "今日暫無相關資訊。"
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config={"max_output_tokens": max_tokens, "temperature": 0.4},
        )
        text = response.text.strip()
        text = re.sub(r"#{1,6}\s*", "", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        return text
    except Exception as e:
        print(f"❌ Gemini 呼叫失敗：{e}")
        return "AI 分析暫時無法使用"

# ── 金十 MCP ──────────────────────────────────────────────
jin10_session_id = None

def jin10_parse_response(r):
    text = r.text.strip()
    if not text:
        return None
    if "data:" in text:
        candidates = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]" and data.startswith("{"):
                    try:
                        candidates.append(json.loads(data))
                    except Exception:
                        pass
        if candidates:
            for c in reversed(candidates):
                if "result" in c or "id" in c:
                    return c
            return candidates[-1]
    try:
        return r.json()
    except Exception:
        return None

def jin10_post(payload):
    global jin10_session_id
    if not JIN10_TOKEN:
        return None
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + JIN10_TOKEN,
            "Accept": "application/json, text/event-stream",
        }
        if jin10_session_id:
            headers["Mcp-Session-Id"] = jin10_session_id
        r = requests.post("https://mcp.jin10.com/mcp", headers=headers, json=payload, timeout=20)
        if "Mcp-Session-Id" in r.headers:
            jin10_session_id = r.headers["Mcp-Session-Id"]
        return jin10_parse_response(r)
    except Exception as e:
        print(f"金十請求錯誤：{e}")
        return None

def jin10_initialize():
    result = jin10_post({
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "clientInfo": {"name": "stock-bot", "version": "1.0"},
            "capabilities": {},
        },
    })
    if result and "result" in result:
        print("金十初始化成功")
        jin10_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return True
    print(f"金十初始化失敗：{str(result)[:200]}")
    return False

def jin10_call(tool, args=None):
    result = jin10_post({
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    })
    if not result:
        return None
    if result.get("result", {}).get("isError"):
        return None
    return result.get("result", {}).get("structuredContent", {})

def fetch_jin10_flash():
    try:
        sc = jin10_call("list_flash")
        return sc.get("data", {}).get("items", [])[:8] if sc else []
    except Exception as e:
        print(f"金十快訊錯誤：{e}")
        return []

def fetch_jin10_search(keyword):
    try:
        sc = jin10_call("search_flash", {"keyword": keyword})
        return sc.get("data", {}).get("items", [])[:5] if sc else []
    except Exception:
        return []

def fetch_jin10_calendar():
    try:
        sc = jin10_call("list_calendar")
        if not sc:
            return []
        items = sc.get("data", [])
        return [i for i in items if i.get("star", 0) >= 2][:5]
    except Exception as e:
        print(f"金十日曆錯誤：{e}")
        return []

def fetch_jin10_quote(code):
    try:
        sc = jin10_call("get_quote", {"code": code})
        if not sc:
            return "無法取得"
        data = sc.get("data", {})
        price = data.get("close", "")
        pct = data.get("ups_percent", "")
        if price:
            try:
                pct_f = float(str(pct).replace("%", ""))
                emoji = "🔴" if pct_f < 0 else "🟢"
                return f"{emoji} {price} ({pct})"
            except Exception:
                return str(price)
        return "無法取得"
    except Exception as e:
        print(f"金十報價錯誤 {code}：{e}")
        return "無法取得"

# ── 大盤數據（Yahoo Finance）────────────────────────────
def fetch_yahoo(symbol: str) -> str:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return "無法取得"
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            pct = (curr - prev) / prev * 100
            emoji = "🔴" if pct < 0 else "🟢"
            return f"{emoji} {curr:,.2f} ({pct:+.2f}%)"
        return "數據不足"
    except Exception as e:
        print(f"Yahoo 錯誤 {symbol}：{e}")
        return "無法取得"

# ── 加密貨幣（CoinGecko）────────────────────────────────
def fetch_crypto() -> dict:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=15,
        )
        if r.status_code != 200:
            return {"BTC": "無法取得", "ETH": "無法取得", "SOL": "無法取得"}
        data = r.json()
        results = {}
        for symbol, cg_id in {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.items():
            if cg_id in data:
                price = data[cg_id]["usd"]
                pct = data[cg_id].get("usd_24h_change", 0)
                emoji = "🔴" if pct < 0 else "🟢"
                results[symbol] = f"{emoji} ${price:,.2f} ({pct:+.2f}%)"
            else:
                results[symbol] = "無法取得"
        return results
    except Exception as e:
        print(f"CoinGecko 錯誤：{e}")
        return {"BTC": "無法取得", "ETH": "無法取得", "SOL": "無法取得"}

# ── 台股籌碼（TWSE 官方 API）────────────────────────────
def fetch_twse_flows() -> dict | None:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    endpoints = [
        "https://openapi.twse.com.tw/v1/fund/T86W",
        "https://openapi.twse.com.tw/v1/fund/MI_INDEX",
    ]
    for endpoint in endpoints:
        try:
            r = requests.get(endpoint, timeout=10, headers=headers)
            r.encoding = "utf-8"
            if r.status_code != 200 or not r.text.strip():
                continue
            data = r.json()
            if not data or not isinstance(data, list):
                continue
            row = data[0]

            def fmt(val):
                try:
                    v = int(str(val).replace(",", "").replace("+", ""))
                    emoji = "🔴" if v < 0 else "🟢"
                    return f"{emoji} {v:+,.0f} 萬元"
                except Exception:
                    return "無法解析"

            foreign = row.get("Foreign_Investor_Net_Buy_Sell") or row.get("FOREIGN_NET_BUY_SELL", "0")
            trust   = row.get("Investment_Trust_Net_Buy_Sell") or row.get("TRUST_NET_BUY_SELL", "0")
            dealer  = row.get("Dealer_Net_Buy_Sell") or row.get("DEALER_NET_BUY_SELL", "0")
            if foreign == "0" and trust == "0":
                continue
            return {"外資": fmt(foreign), "投信": fmt(trust), "自營商": fmt(dealer)}
        except Exception as e:
            print(f"TWSE 錯誤：{e}")
            continue
    return None

# ── 新聞抓取（含時段過濾）────────────────────────────────
def fetch_newsapi_tw() -> list:
    if not NEWS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://newsapi.org/v2/top-headlines",
            params={"country": "tw", "category": "business", "pageSize": 10, "apiKey": NEWS_API_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [
            {"title": a["title"], "url": clean_url(a.get("url", "#"))}
            for a in r.json().get("articles", [])
            if a.get("title") and "[Removed]" not in a.get("title", "")
        ]
    except Exception:
        return []

def fetch_gnews(query: str, lang: str = "zh-TW", count: int = 8) -> list:
    if not GNEWS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://gnews.io/api/v4/search",
            params={"q": query, "lang": lang, "max": count, "apikey": GNEWS_API_KEY, "sortby": "publishedAt"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [
            {"title": a.get("title", ""), "url": clean_url(a.get("url", "#"))}
            for a in r.json().get("articles", [])
            if a.get("title")
        ]
    except Exception:
        return []

def fetch_finnhub_news(category: str = "general") -> list:
    if not FINNHUB_KEY:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": category, "token": FINNHUB_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        items = r.json() if isinstance(r.json(), list) else []
        return [
            {"title": i.get("headline", ""), "url": clean_url(i.get("url", "#"))}
            for i in items[:10]
            if i.get("headline")
        ]
    except Exception:
        return []

def fetch_newsdata(query: str, lang: str = "zh") -> list:
    if not NEWSDATA_API_KEY:
        return []
    try:
        r = requests.get(
            "https://newsdata.io/api/1/news",
            params={"apikey": NEWSDATA_API_KEY, "q": query, "language": lang, "category": "business"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return [
            {"title": i.get("title", ""), "url": clean_url(i.get("link", "#"))}
            for i in r.json().get("results", [])[:10]
            if i.get("title")
        ]
    except Exception:
        return []

def merge_news(lists: list, limit: int = 10) -> list:
    seen = set()
    merged = []
    for lst in lists:
        for a in lst:
            t = a.get("title", "")
            if t and t not in seen and len(t) > 5:
                seen.add(t)
                merged.append(a)
    return merged[:limit]

def build_links(articles: list, limit: int = 8) -> str:
    """組合新聞連結，若有 Gemini 則翻譯標題，否則直接用原標題"""
    if not articles:
        return "暫無新聞"
    articles = articles[:limit]

    if gemini_client:
        titles_text = "\n".join([f"{i+1}. {a['title']}" for i, a in enumerate(articles)])
        translated = gemini_call(
            "請將以下新聞標題逐條翻譯成繁體中文，只輸出編號+翻譯結果，每行一條，不加任何說明：\n" + titles_text,
            max_tokens=500,
        )
        trans_list = []
        if translated and "無法使用" not in translated:
            trans_list = [l.strip() for l in translated.strip().split("\n") if l.strip()]
    else:
        trans_list = []

    lines = []
    for i, a in enumerate(articles):
        url = a.get("url", "#")
        if i < len(trans_list):
            t = re.sub(r"^\d+[\.\s．]+", "", trans_list[i]).strip()
        else:
            t = a["title"][:40]
        t = t[:45]
        lines.append(f"{i+1}. [{t}]({url})")
    return "\n".join(lines)

def make_flash_text(items: list) -> str:
    if not items:
        return "暫無快訊"
    lines = []
    for item in items[:6]:
        t = item.get("title", "") or item.get("content", "")
        if t:
            t = re.sub(r"#{1,6}\s*", "", str(t))
            lines.append("• " + t[:70])
    return "\n".join(lines) if lines else "暫無快訊"

def make_calendar_text(items: list) -> str:
    if not items:
        return "今日無重要財經數據"
    lines = []
    for item in items:
        stars = "⭐" * min(int(item.get("star", 0)), 3)
        title = item.get("title", "")[:30]
        pub_time = str(item.get("pub_time", ""))[:16]
        actual = item.get("actual", "-")
        consensus = item.get("consensus", "-")
        lines.append(f"{stars} {pub_time} {title} 預期:{consensus} 實際:{actual}")
    return "\n".join(lines[:4])

# ── Discord 傳送 ──────────────────────────────────────────
def send_embed(webhook_url: str, embed: dict) -> None:
    for field in embed.get("fields", []):
        v = field.get("value", "")
        if not v or not v.strip():
            field["value"] = "暫無資料"
        elif len(v) > 1024:
            field["value"] = v[:1021] + "..."
    try:
        res = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        if res.status_code in [200, 204]:
            print(f"✅ 發送成功：{embed.get('title', '')}")
        else:
            print(f"❌ 發送失敗：{res.status_code} {res.text[:300]}")
    except Exception as e:
        print(f"❌ 發送錯誤：{e}")
    time.sleep(1.2)

# ── 主程式 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    session_label = get_session_label()
    print(f"🚀 股市日報啟動 — {now_str()}")
    print(f"📅 本次時段：{session_label}")
    print("=" * 60)
    now = now_str()

    # 金十初始化
    print("金十初始化...")
    jin10_ok = jin10_initialize() if JIN10_TOKEN else False

    # 大盤數據
    print("抓取大盤數據...")
    twii  = fetch_yahoo("^TWII")
    dji   = fetch_yahoo("^DJI")
    ixic  = fetch_yahoo("^IXIC")
    gspc  = fetch_yahoo("^GSPC")
    vix   = fetch_yahoo("^VIX")
    us10y = fetch_yahoo("^TNX")
    dxy   = fetch_yahoo("DX-Y.NYB")
    gold  = fetch_yahoo("GC=F")
    oil   = fetch_yahoo("CL=F")

    if jin10_ok:
        print("抓取金十報價...")
        gold_j = fetch_jin10_quote("XAUUSD")
        oil_j  = fetch_jin10_quote("USOIL")
        if gold_j != "無法取得":
            gold = gold_j
        if oil_j != "無法取得":
            oil = oil_j

    # 加密貨幣
    print("抓取加密貨幣...")
    crypto = fetch_crypto()
    btc = crypto["BTC"]
    eth = crypto["ETH"]
    sol = crypto["SOL"]

    # 台股籌碼
    print("抓取台股籌碼...")
    flows = fetch_twse_flows()

    # 金十快訊與日曆
    print("抓取金十快訊與日曆...")
    jin10_items = fetch_jin10_flash() if jin10_ok else []
    if jin10_ok:
        extra_flash = []
        for kw in ["美联储", "黄金", "原油", "非农"]:
            extra_flash += fetch_jin10_search(kw)
            time.sleep(0.3)
        for item in extra_flash:
            if item not in jin10_items:
                jin10_items.append(item)
        jin10_items = jin10_items[:10]
    calendar_items = fetch_jin10_calendar() if jin10_ok else []

    # 新聞抓取
    print("抓取新聞...")
    tw_news = merge_news([
        fetch_newsapi_tw(),
        fetch_gnews("台股 股市 台灣經濟", "zh-TW", 8),
        fetch_gnews("台灣 財經 科技 企業", "zh-TW", 8),
        fetch_newsdata("台股 股市", "zh"),
        fetch_newsdata("taiwan stock economy", "en"),
    ], limit=10)

    us_news = merge_news([
        fetch_finnhub_news("general"),
        fetch_gnews("US stock market Fed earnings", "en", 8),
        fetch_gnews("Wall Street NASDAQ S&P500 economy", "en", 8),
        fetch_newsdata("US stock market economy", "en"),
    ], limit=10)

    global_news = merge_news([
        fetch_finnhub_news("forex"),
        fetch_gnews("war oil OPEC geopolitics economy", "en", 8),
        fetch_gnews("Fed inflation global trade", "en", 8),
        fetch_newsdata("war economy oil geopolitics", "en"),
    ], limit=10)

    print(f"  台股新聞：{len(tw_news)} 篇 | 美股：{len(us_news)} 篇 | 國際：{len(global_news)} 篇")

    # Gemini 翻譯新聞標題
    print("Gemini 翻譯新聞標題...")
    tw_links     = build_links(tw_news, limit=8)
    time.sleep(0.5)
    us_links     = build_links(us_news, limit=8)
    time.sleep(0.5)
    global_links = build_links(global_news, limit=8)
    time.sleep(0.5)

    # Gemini 深度分析
    print("Gemini 深度分析...")
    tw_content     = "\n".join([a["title"] for a in tw_news[:10]]) or "（無新聞）"
    us_content     = "\n".join([a["title"] for a in us_news[:10]]) or "（無新聞）"
    flash_text     = make_flash_text(jin10_items)
    global_content = flash_text + "\n" + ("\n".join([a["title"] for a in global_news[:8]]) or "（無新聞）")

    tw_analysis = gemini_call(
        f"以下是{session_label}的台股新聞，用繁體中文寫3-4個重點，每點•開頭，每點2句話，不超過200字，不用###或**：\n\n{tw_content}",
        max_tokens=600,
    )
    us_analysis = gemini_call(
        f"以下是{session_label}的美股新聞，用繁體中文寫3-4個重點，每點•開頭，每點2句話，不超過200字，不用###或**：\n\n{us_content}",
        max_tokens=600,
    )
    global_analysis = gemini_call(
        f"以下是{session_label}的全球財經快訊，用繁體中文寫3-4個重點，每點•開頭，每點2句話，不超過200字，不用###或**：\n\n{global_content}",
        max_tokens=600,
    )
    market_sentiment = gemini_call(
        "用繁體中文2-3句話分析以下指標對今日市場情緒的影響，不要標題或bullet：\n"
        f"VIX:{vix} 美債10Y:{us10y} DXY:{dxy} 黃金:{gold} 原油:{oil} BTC:{btc} ETH:{eth}",
        max_tokens=200,
    )

    if flows:
        flows_text = f"外資：{flows['外資']}\n投信：{flows['投信']}\n自營商：{flows['自營商']}"
    else:
        flows_text = "今日籌碼資料暫無法取得"

    # ── 發送到 Discord ────────────────────────────────────
    print("發送到 Discord...")
    session_tag = f"[{session_label}]"

    # 台股 - 兩則
    send_embed(DISCORD_TW, {
        "title": f"🇹🇼 台股市場 {session_tag} | {now}",
        "color": 3066993,
        "fields": [
            {"name": "📊 加權指數", "value": safe_val(f"台灣加權：{twii}"), "inline": False},
            {"name": "🏦 法人籌碼", "value": safe_val(flows_text), "inline": False},
            {"name": "🤖 AI 深度分析", "value": safe_val(tw_analysis, 1000), "inline": False},
        ],
    })
    send_embed(DISCORD_TW, {
        "title": f"📰 台股新聞 {session_tag} | {now}",
        "color": 2067276,
        "fields": [
            {"name": "🔗 新聞連結（Gemini翻譯）", "value": safe_val(tw_links, 1000), "inline": False},
        ],
    })

    # 美股 - 兩則
    send_embed(DISCORD_US, {
        "title": f"🇺🇸 美股市場 {session_tag} | {now}",
        "color": 3447003,
        "fields": [
            {"name": "📊 大盤指數", "value": safe_val(f"道瓊：{dji}\n納斯達克：{ixic}\nS&P500：{gspc}"), "inline": False},
            {"name": "🤖 AI 深度分析", "value": safe_val(us_analysis, 1000), "inline": False},
        ],
    })
    send_embed(DISCORD_US, {
        "title": f"📰 美股新聞 {session_tag} | {now}",
        "color": 1127128,
        "fields": [
            {"name": "🔗 新聞連結（Gemini翻譯）", "value": safe_val(us_links, 1000), "inline": False},
        ],
    })

    # 國際 - 三則
    send_embed(DISCORD_GLOBAL, {
        "title": f"� 全球市場 {session_tag} | {now}",
        "color": 10181046,
        "fields": [
            {"name": "📉 總體指標", "value": safe_val(f"VIX：{vix}\n美債10Y：{us10y}\nDXY：{dxy}\n黃金：{gold}\n原油：{oil}"), "inline": True},
            {"name": "🪙 加密貨幣", "value": safe_val(f"BTC：{btc}\nETH：{eth}\nSOL：{sol}"), "inline": True},
            {"name": "🌡 市場情緒", "value": safe_val(market_sentiment, 500), "inline": False},
        ],
    })
    send_embed(DISCORD_GLOBAL, {
        "title": f"⚡ 金十快訊 & 財經日曆 {session_tag} | {now}",
        "color": 15844367,
        "fields": [
            {"name": "⚡ 最新快訊", "value": safe_val(flash_text, 1000), "inline": False},
            {"name": "📅 財經日曆", "value": safe_val(make_calendar_text(calendar_items), 600), "inline": False},
        ],
    })
    send_embed(DISCORD_GLOBAL, {
        "title": f"📰 全球新聞 {session_tag} | {now}",
        "color": 9807270,
        "fields": [
            {"name": "🤖 AI 深度分析", "value": safe_val(global_analysis, 1000), "inline": False},
            {"name": "🔗 新聞連結（Gemini翻譯）", "value": safe_val(global_links, 1000), "inline": False},
        ],
    })

    print("=" * 60)
    print("✅ 完成！")
    print("=" * 60)
