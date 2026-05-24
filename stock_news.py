import requests
import os
import time
from datetime import datetime, timezone, timedelta

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GNEWS_API_KEY = os.environ["GNEWS_API_KEY"]
JIN10_TOKEN = os.environ["JIN10_TOKEN"]
DISCORD_TW = os.environ["DISCORD_TW"]
DISCORD_US = os.environ["DISCORD_US"]
DISCORD_GLOBAL = os.environ["DISCORD_GLOBAL"]

TW_TZ = timezone(timedelta(hours=8))

def now_tw():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

def fetch_yahoo(symbol):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?interval=1d&range=2d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            pct = (curr - prev) / prev * 100
            emoji = "\U0001f534" if pct < 0 else "\U0001f7e2"
            return emoji + " " + "{:,.2f}".format(curr) + " (" + "{:+.2f}".format(pct) + "%)"
        return "數據不足"
    except Exception:
        return "無法取得"

def fetch_crypto(symbol):
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr?symbol=" + symbol
        r = requests.get(url, timeout=10)
        data = r.json()
        price = float(data["lastPrice"])
        pct = float(data["priceChangePercent"])
        emoji = "\U0001f534" if pct < 0 else "\U0001f7e2"
        return emoji + " $" + "{:,.2f}".format(price) + " (" + "{:+.2f}".format(pct) + "%)"
    except Exception:
        return "無法取得"

def fetch_twse_flows():
    try:
        url = "https://openapi.twse.com.tw/v1/fund/T86W"
        r = requests.get(url, timeout=10)
        data = r.json()
        if not data:
            return None
        row = data[0]
        def fmt(val):
            try:
                v = int(str(val).replace(",", ""))
                emoji = "\U0001f534" if v < 0 else "\U0001f7e2"
                return emoji + " " + "{:+,.0f}".format(v) + " 萬元"
            except Exception:
                return "無法解析"
        return {
            "外資": fmt(row.get("Foreign_Investor_Net_Buy_Sell", "0")),
            "投信": fmt(row.get("Investment_Trust_Net_Buy_Sell", "0")),
            "自營商": fmt(row.get("Dealer_Net_Buy_Sell", "0"))
        }
    except Exception:
        return None

def jin10_mcp(method, params=None):
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        r = requests.post(
            "https://mcp.jin10.com/mcp",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + JIN10_TOKEN
            },
            json=payload,
            timeout=15
        )
        return r.json()
    except Exception as e:
        print("金十API錯誤：" + str(e))
        return None

def fetch_jin10_flash():
    try:
        result = jin10_mcp("tools/call", {"name": "list_flash", "arguments": {}})
        if not result:
            return []
        items = result.get("result", {}).get("structuredContent", {}).get("data", {}).get("items", [])
        return items[:8]
    except Exception:
        return []

def fetch_jin10_calendar():
    try:
        result = jin10_mcp("tools/call", {"name": "list_calendar", "arguments": {}})
        if not result:
            return []
        items = result.get("result", {}).get("structuredContent", {}).get("data", [])
        important = [i for i in items if i.get("star", 0) >= 2]
        return important[:6]
    except Exception:
        return []

def fetch_jin10_quote(code):
    try:
        result = jin10_mcp("tools/call", {"name": "get_quote", "arguments": {"code": code}})
        if not result:
            return "無法取得"
        data = result.get("result", {}).get("structuredContent", {}).get("data", {})
        price = data.get("close", "")
        pct = data.get("ups_percent", "")
        if price:
            try:
                pct_f = float(str(pct).replace("%",""))
                emoji = "\U0001f534" if pct_f < 0 else "\U0001f7e2"
                return emoji + " " + str(price) + " (" + str(pct) + ")"
            except Exception:
                return str(price)
        return "無法取得"
    except Exception:
        return "無法取得"

def fetch_gnews(query, lang="zh-TW", count=5):
    try:
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": query,
            "lang": lang,
            "max": count,
            "apikey": GNEWS_API_KEY,
            "sortby": "publishedAt"
        }
        r = requests.get(url, params=params, timeout=10)
        articles = r.json().get("articles", [])
        return articles
    except Exception:
        return []

def claude_analyze(text, prompt_prefix, max_tokens=300):
    try:
        prompt = prompt_prefix + "\n\n" + text
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = res.json()
        if "content" in result:
            return result["content"][0]["text"].strip()[:900]
        return "AI分析暫時無法使用"
    except Exception:
        return "AI分析暫時無法使用"

def translate_title(title):
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 80,
                "messages": [{"role": "user", "content": "請將以下新聞標題翻譯成繁體中文，只輸出翻譯結果，不要加任何說明：\n" + title}]
            },
            timeout=15
        )
        result = res.json()
        if "content" in result:
            return result["content"][0]["text"].strip()
        return title
    except Exception:
        return title

def make_news_links(articles, translate=False):
    if not articles:
        return "暫無新聞連結"
    lines = []
    for i, a in enumerate(articles[:5]):
        title = a.get("title", "")
        url = a.get("url", "#")
        if translate and title:
            title = translate_title(title)
            time.sleep(0.3)
        title = title[:40] + "..." if len(title) > 40 else title
        lines.append(str(i+1) + ". [" + title + "](" + url + ")")
    return "\n".join(lines)

def make_flash_text(items):
    if not items:
        return "暫無快訊"
    lines = []
    for item in items[:6]:
        title = item.get("title", "") or item.get("content", "")
        if title:
            lines.append("• " + str(title)[:60])
    return "\n".join(lines) if lines else "暫無快訊"

def make_calendar_text(items):
    if not items:
        return "今日無重要財經數據"
    lines = []
    for item in items:
        star = int(item.get("star", 0))
        stars = "\u2b50" * min(star, 3)
        title = item.get("title", "")
        pub_time = item.get("pub_time", "")[:16]
        actual = item.get("actual", "-")
        consensus = item.get("consensus", "-")
        lines.append(stars + " " + pub_time + " " + title + " | 預期:" + str(consensus) + " 實際:" + str(actual))
    return "\n".join(lines[:5]) if lines else "今日無重要財經數據"

def send_embed(webhook_url, embed):
    try:
        res = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        if res.status_code in [200, 204]:
            print("發送成功：" + embed.get("title", ""))
        else:
            print("發送失敗：" + str(res.status_code) + " " + res.text[:200])
    except Exception as e:
        print("發送錯誤：" + str(e))
    time.sleep(1)

print("開始執行...")
now = now_tw()

print("抓取大盤數據...")
twii = fetch_yahoo("^TWII")
dji = fetch_yahoo("^DJI")
ixic = fetch_yahoo("^IXIC")
gspc = fetch_yahoo("^GSPC")
vix = fetch_yahoo("^VIX")
us10y = fetch_yahoo("^TNX")
dxy = fetch_yahoo("DX-Y.NYB")

print("抓取金十報價...")
gold = fetch_jin10_quote("XAUUSD")
oil = fetch_jin10_quote("USOIL")

print("抓取加密貨幣...")
btc = fetch_crypto("BTCUSDT")
eth = fetch_crypto("ETHUSDT")
sol = fetch_crypto("SOLUSDT")

print("抓取台股籌碼...")
flows = fetch_twse_flows()

print("抓取金十快訊與日曆...")
jin10_items = fetch_jin10_flash()
calendar_items = fetch_jin10_calendar()

print("抓取新聞...")
tw_articles = fetch_gnews("台股 股市 台灣經濟", "zh-TW", 5)
us_articles = fetch_gnews("US stock market Wall Street Trump", "en", 5)
global_articles = fetch_gnews("war Ukraine economy Fed oil geopolitics", "en", 5)

print("AI 分析中...")
tw_titles = "\n".join([str(i+1) + ". " + a.get("title","") for i, a in enumerate(tw_articles)])
us_titles = "\n".join([str(i+1) + ". " + a.get("title","") for i, a in enumerate(us_articles)])
global_titles = "\n".join([str(i+1) + ". " + a.get("title","") for i, a in enumerate(global_articles)])
flash_text = make_flash_text(jin10_items)

tw_analysis = claude_analyze(
    tw_titles,
    "以下是今日台股新聞，請用繁體中文寫2-3個重點，每點用•開頭，總字數不超過100字，不要用###標題："
)
us_analysis = claude_analyze(
    us_titles,
    "以下是今日美股新聞，請用繁體中文寫2-3個重點，每點用•開頭，總字數不超過100字，不要用###標題："
)
global_analysis = claude_analyze(
    flash_text + "\n\n" + global_titles,
    "以下是今日全球財經快訊，請用繁體中文寫2-3個重點，每點用•開頭，總字數不超過100字，不要用###標題："
)
market_sentiment = claude_analyze(
    "VIX:" + vix + " 美債10Y:" + us10y + " DXY:" + dxy + " BTC:" + btc + " ETH:" + eth,
    "請用繁體中文一句話（不超過60字）分析以上指標對今日市場情緒的影響，不要標題："
)

if flows:
    flows_text = "外資：" + flows["外資"] + "\n投信：" + flows["投信"] + "\n自營商：" + flows["自營商"]
else:
    flows_text = "今日籌碼資料暫無法取得"

print("翻譯新聞標題...")
tw_links = make_news_links(tw_articles, translate=False)
us_links = make_news_links(us_articles, translate=True)
global_links = make_news_links(global_articles, translate=True)

print("發送到 Discord...")

send_embed(DISCORD_TW, {
    "title": "\U0001f1f9\U0001f1fc 台股市場 | " + now,
    "color": 3066993,
    "fields": [
        {"name": "\U0001f4ca 加權指數", "value": "台灣加權：" + twii, "inline": False},
        {"name": "\U0001f3e6 法人籌碼", "value": flows_text, "inline": False},
        {"name": "\U0001f916 AI 分析重點", "value": tw_analysis, "inline": False},
        {"name": "\U0001f4f0 相關新聞", "value": tw_links, "inline": False}
    ]
})

send_embed(DISCORD_US, {
    "title": "\U0001f1fa\U0001f1f8 美股市場 | " + now,
    "color": 3447003,
    "fields": [
        {"name": "\U0001f4ca 大盤指數", "value": "道瓊：" + dji + "\n納斯達克：" + ixic + "\nS&P500：" + gspc, "inline": False},
        {"name": "\U0001f916 AI 分析重點", "value": us_analysis, "inline": False},
        {"name": "\U0001f4f0 相關新聞", "value": us_links, "inline": False}
    ]
})

send_embed(DISCORD_GLOBAL, {
    "title": "\U0001f310 全球市場 | " + now,
    "color": 10181046,
    "fields": [
        {"name": "\U0001f4c9 總體指標", "value": "VIX：" + vix + "\n美債10Y：" + us10y + "\nDXY：" + dxy + "\n黃金：" + gold + "\n原油：" + oil, "inline": True},
        {"name": "\U0001fa99 加密貨幣", "value": "BTC：" + btc + "\nETH：" + eth + "\nSOL：" + sol, "inline": True},
        {"name": "\u26a1 金十快訊", "value": flash_text, "inline": False},
        {"name": "\U0001f4c5 財經日曆", "value": make_calendar_text(calendar_items), "inline": False},
        {"name": "\U0001f916 AI 分析", "value": global_analysis, "inline": False},
        {"name": "\U0001f321 市場情緒", "value": market_sentiment, "inline": False},
        {"name": "\U0001f4f0 相關新聞", "value": global_links, "inline": False}
    ]
})

print("完成！")
