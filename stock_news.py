import requests
import os
import time
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, urlencode, parse_qs

# 環境變數
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GH_MODELS_TOKEN = os.environ["GH_MODELS_TOKEN"]
NEWS_API_KEY = os.environ["NEWS_API_KEY"]
GNEWS_API_KEY = os.environ["GNEWS_API_KEY"]
FINNHUB_KEY = os.environ["FINNHUB_KEY"]
NEWSDATA_API_KEY = os.environ["NEWSDATA_API_KEY"]
JIN10_TOKEN = os.environ["JIN10_TOKEN"]
DISCORD_TW = os.environ["DISCORD_TW"]
DISCORD_US = os.environ["DISCORD_US"]
DISCORD_GLOBAL = os.environ["DISCORD_GLOBAL"]

TW_TZ = timezone(timedelta(hours=8))

def now_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

def clean_url(url):
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except Exception:
        return url

# GPT-5 翻譯
def gpt5_translate(titles_text):
    try:
        res = requests.post(
            "https://models.github.ai/inference/chat/completions",
            headers={"Authorization": "Bearer " + GH_MODELS_TOKEN, "Content-Type": "application/json"},
            json={
                "model": "openai/gpt-5",
                "messages": [
                    {"role": "system", "content": "你是專業財經翻譯，只輸出翻譯結果，不加說明。"},
                    {"role": "user", "content": "請將以下新聞標題逐條翻譯成繁體中文，只輸出編號+翻譯，每行一條：\n" + titles_text}
                ],
                "max_tokens": 400
            },
            timeout=30
        )
        if res.status_code == 200:
            data = res.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
        print("GPT-5翻譯失敗：" + str(res.status_code))
        return None
    except Exception as e:
        print("GPT-5錯誤：" + str(e))
        return None

# Claude 分析
def claude_analyze(content, prompt, max_tokens=400):
    if not content or content.strip() == "":
        return "今日暫無相關資訊。"
    try:
        full_prompt = prompt + "\n\n" + content
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-5", "max_tokens": max_tokens, "messages": [{"role": "user", "content": full_prompt}]},
            timeout=30
        )
        if res.status_code == 200:
            result = res.json()
            if "content" in result:
                text = result["content"][0]["text"].strip()
                text = re.sub(r'#{1,6}\s*', '', text)
                text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
                return text[:900]
        return "AI分析暫時無法使用"
    except Exception as e:
        print("Claude錯誤：" + str(e))
        return "AI分析暫時無法使用"

# 大盤數據
def fetch_yahoo(symbol):
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return "無法取得"
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            pct = (curr - prev) / prev * 100
            emoji = "\U0001f534" if pct < 0 else "\U0001f7e2"
            return emoji + " " + "{:,.2f}".format(curr) + " (" + "{:+.2f}".format(pct) + "%)"
        return "數據不足"
    except Exception as e:
        print("Yahoo錯誤 " + symbol + "：" + str(e))
        return "無法取得"

# CoinGecko 加密貨幣
def fetch_crypto():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return {"BTC": "無法取得", "ETH": "無法取得", "SOL": "無法取得"}
        data = r.json()
        results = {}
        mapping = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
        for symbol, cg_id in mapping.items():
            if cg_id in data:
                price = data[cg_id]["usd"]
                pct = data[cg_id].get("usd_24h_change", 0)
                emoji = "\U0001f534" if pct < 0 else "\U0001f7e2"
                results[symbol] = emoji + " $" + "{:,.2f}".format(price) + " (" + "{:+.2f}".format(pct) + "%)"
            else:
                results[symbol] = "無法取得"
        return results
    except Exception as e:
        print("CoinGecko錯誤：" + str(e))
        return {"BTC": "無法取得", "ETH": "無法取得", "SOL": "無法取得"}

# 台股籌碼
def fetch_twse_flows():
    try:
        url = "https://openapi.twse.com.tw/v1/fund/T86W"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
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
    except Exception as e:
        print("TWSE籌碼錯誤：" + str(e))
        return None

# 金十數據
def jin10_call(tool_name, arguments=None):
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}}
        r = requests.post(
            "https://mcp.jin10.com/mcp",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + JIN10_TOKEN},
            json=payload, timeout=15
        )
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        print("金十錯誤：" + str(e))
        return None

def fetch_jin10_flash():
    try:
        result = jin10_call("list_flash")
        if not result:
            return []
        items = result.get("result", {}).get("structuredContent", {}).get("data", {}).get("items", [])
        return items[:8]
    except Exception:
        return []

def fetch_jin10_calendar():
    try:
        result = jin10_call("list_calendar")
        if not result:
            return []
        items = result.get("result", {}).get("structuredContent", {}).get("data", [])
        return [i for i in items if i.get("star", 0) >= 2][:5]
    except Exception:
        return []

def fetch_jin10_quote(code):
    try:
        result = jin10_call("get_quote", {"code": code})
        if not result:
            return "無法取得"
        data = result.get("result", {}).get("structuredContent", {}).get("data", {})
        price = data.get("close", "")
        pct = data.get("ups_percent", "")
        if price:
            try:
                pct_f = float(str(pct).replace("%", ""))
                emoji = "\U0001f534" if pct_f < 0 else "\U0001f7e2"
                return emoji + " " + str(price) + " (" + str(pct) + ")"
            except Exception:
                return str(price)
        return "無法取得"
    except Exception:
        return "無法取得"

# 新聞抓取（回傳 list of {title, url}）
def fetch_newsapi_tw():
    try:
        r = requests.get("https://newsapi.org/v2/top-headlines",
            params={"country": "tw", "category": "business", "pageSize": 8, "apiKey": NEWS_API_KEY}, timeout=10)
        if r.status_code != 200:
            return []
        articles = r.json().get("articles", [])
        return [{"title": a["title"], "url": clean_url(a.get("url","#"))} for a in articles if a.get("title") and "[Removed]" not in a.get("title","")]
    except Exception:
        return []

def fetch_gnews(query, lang="zh-TW", count=8):
    try:
        r = requests.get("https://gnews.io/api/v4/search",
            params={"q": query, "lang": lang, "max": count, "apikey": GNEWS_API_KEY, "sortby": "publishedAt"}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"title": a.get("title",""), "url": clean_url(a.get("url","#"))} for a in r.json().get("articles", []) if a.get("title")]
    except Exception:
        return []

def fetch_finnhub_news(category="general"):
    try:
        r = requests.get("https://finnhub.io/api/v1/news",
            params={"category": category, "token": FINNHUB_KEY}, timeout=10)
        if r.status_code != 200:
            return []
        items = r.json() if isinstance(r.json(), list) else []
        return [{"title": i.get("headline",""), "url": clean_url(i.get("url","#"))} for i in items[:8] if i.get("headline")]
    except Exception:
        return []

def fetch_newsdata(query, lang="zh"):
    try:
        r = requests.get("https://newsdata.io/api/1/news",
            params={"apikey": NEWSDATA_API_KEY, "q": query, "language": lang, "category": "business"}, timeout=10)
        if r.status_code != 200:
            return []
        return [{"title": i.get("title",""), "url": clean_url(i.get("link","#"))} for i in r.json().get("results", [])[:8] if i.get("title")]
    except Exception:
        return []

def merge_news(lists, limit=10):
    seen = set()
    merged = []
    for lst in lists:
        for a in lst:
            t = a.get("title", "")
            if t and t not in seen and len(t) > 5:
                seen.add(t)
                merged.append(a)
    return merged[:limit]

# 翻譯並產生連結（中文標題 + URL）
def build_links(articles):
    if not articles:
        return "暫無新聞", []
    titles_text = "\n".join([str(i+1) + ". " + a["title"] for i, a in enumerate(articles[:8])])
    translated = gpt5_translate(titles_text)
    lines = []
    refs = []
    trans_list = []
    if translated:
        trans_list = [l.strip() for l in translated.strip().split("\n") if l.strip()]
    for i, a in enumerate(articles[:8]):
        url = a.get("url", "#")
        if i < len(trans_list):
            t = re.sub(r'^\d+[\.\s]+', '', trans_list[i]).strip()
        else:
            t = a["title"][:35] + "..."
        t = t[:45]
        lines.append(str(i+1) + ". [" + t + "](" + url + ")")
        refs.append({"title": t, "url": url})
    return "\n".join(lines), refs

def make_flash_text(items):
    if not items:
        return "暫無快訊"
    lines = []
    for item in items[:6]:
        t = item.get("title", "") or item.get("content", "")
        if t:
            t = re.sub(r'#{1,6}\s*', '', t)
            lines.append("\u2022 " + str(t)[:70])
    return "\n".join(lines) if lines else "暫無快訊"

def make_calendar_text(items):
    if not items:
        return "今日無重要財經數據"
    lines = []
    for item in items:
        stars = "\u2b50" * min(int(item.get("star", 0)), 3)
        title = item.get("title", "")
        pub_time = str(item.get("pub_time", ""))[:16]
        actual = item.get("actual", "-")
        consensus = item.get("consensus", "-")
        lines.append(stars + " " + pub_time + " " + title + " | 預期:" + str(consensus) + " 實際:" + str(actual))
    return "\n".join(lines[:5])

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

# 主流程
print("開始執行...")
now = now_str()

print("抓取大盤數據...")
twii = fetch_yahoo("^TWII")
dji  = fetch_yahoo("^DJI")
ixic = fetch_yahoo("^IXIC")
gspc = fetch_yahoo("^GSPC")
vix  = fetch_yahoo("^VIX")
us10y = fetch_yahoo("^TNX")
dxy  = fetch_yahoo("DX-Y.NYB")
gold_y = fetch_yahoo("GC=F")
oil_y  = fetch_yahoo("CL=F")

print("抓取金十報價...")
gold = fetch_jin10_quote("XAUUSD")
oil  = fetch_jin10_quote("USOIL")
if gold == "無法取得":
    gold = gold_y
if oil == "無法取得":
    oil = oil_y

print("抓取加密貨幣...")
crypto = fetch_crypto()
btc = crypto["BTC"]
eth = crypto["ETH"]
sol = crypto["SOL"]

print("抓取台股籌碼...")
flows = fetch_twse_flows()

print("抓取金十快訊與日曆...")
jin10_items = fetch_jin10_flash()
calendar_items = fetch_jin10_calendar()

print("抓取新聞...")
tw_news = merge_news([
    fetch_newsapi_tw(),
    fetch_gnews("台股 股市 台灣經濟 科技", "zh-TW", 8),
    fetch_gnews("台灣 財經 企業", "zh-TW", 5),
    fetch_newsdata("台股 股市 台灣", "zh"),
    fetch_newsdata("taiwan stock economy", "en")
], limit=10)

us_news = merge_news([
    fetch_finnhub_news("general"),
    fetch_gnews("US stock market Trump Fed Wall Street earnings", "en", 8),
    fetch_gnews("S&P500 NASDAQ Dow Jones", "en", 5),
    fetch_newsdata("US stock market economy", "en")
], limit=10)

global_news = merge_news([
    fetch_finnhub_news("forex"),
    fetch_gnews("war Ukraine Russia geopolitics oil OPEC sanctions", "en", 8),
    fetch_gnews("Fed interest rate inflation economy global", "en", 5),
    fetch_newsdata("war economy oil geopolitics", "en")
], limit=10)

print("GPT-5 翻譯新聞標題...")
tw_links, tw_refs = build_links(tw_news)
time.sleep(1)
us_links, us_refs = build_links(us_news)
time.sleep(1)
global_links, global_refs = build_links(global_news)
time.sleep(1)

print("Claude 深度分析...")
tw_content = "\n".join([a["title"] for a in tw_news[:8]])
us_content = "\n".join([a["title"] for a in us_news[:8]])
global_content = make_flash_text(jin10_items) + "\n" + "\n".join([a["title"] for a in global_news[:6]])

tw_analysis = claude_analyze(tw_content,
    "以下是今日台股新聞，請用繁體中文寫2-3個重點，每點用•開頭，不超過150字，不要用###或**：")
us_analysis = claude_analyze(us_content,
    "以下是今日美股新聞，請用繁體中文寫2-3個重點，每點用•開頭，不超過150字，不要用###或**：")
global_analysis = claude_analyze(global_content,
    "以下是今日全球財經快訊，請用繁體中文寫2-3個重點，每點用•開頭，不超過150字，不要用###或**：")
market_sentiment = claude_analyze(
    "VIX:" + vix + " 美債10Y:" + us10y + " DXY:" + dxy + " 黃金:" + gold + " 原油:" + oil + " BTC:" + btc + " ETH:" + eth,
    "請用繁體中文一句話（不超過60字）分析以上指標對今日市場情緒的影響，不要標題或bullet：")

if flows:
    flows_text = "外資：" + flows["外資"] + "\n投信：" + flows["投信"] + "\n自營商：" + flows["自營商"]
else:
    flows_text = "今日籌碼資料暫無法取得"

print("發送到 Discord...")

send_embed(DISCORD_TW, {
    "title": "\U0001f1f9\U0001f1fc 台股市場 | " + now,
    "color": 3066993,
    "fields": [
        {"name": "\U0001f4ca 加權指數", "value": "台灣加權：" + twii, "inline": False},
        {"name": "\U0001f3e6 法人籌碼", "value": flows_text, "inline": False},
        {"name": "\U0001f916 AI 分析（Claude）", "value": tw_analysis, "inline": False},
        {"name": "\U0001f4f0 今日新聞（GPT-5翻譯）", "value": tw_links, "inline": False}
    ]
})

send_embed(DISCORD_US, {
    "title": "\U0001f1fa\U0001f1f8 美股市場 | " + now,
    "color": 3447003,
    "fields": [
        {"name": "\U0001f4ca 大盤指數", "value": "道瓊：" + dji + "\n納斯達克：" + ixic + "\nS&P500：" + gspc, "inline": False},
        {"name": "\U0001f916 AI 分析（Claude）", "value": us_analysis, "inline": False},
        {"name": "\U0001f4f0 今日新聞（GPT-5翻譯）", "value": us_links, "inline": False}
    ]
})

send_embed(DISCORD_GLOBAL, {
    "title": "\U0001f310 全球市場 | " + now,
    "color": 10181046,
    "fields": [
        {"name": "\U0001f4c9 總體指標", "value": "VIX：" + vix + "\n美債10Y：" + us10y + "\nDXY：" + dxy + "\n黃金：" + gold + "\n原油：" + oil, "inline": True},
        {"name": "\U0001fa99 加密貨幣", "value": "BTC：" + btc + "\nETH：" + eth + "\nSOL：" + sol, "inline": True},
        {"name": "\u26a1 金十快訊", "value": make_flash_text(jin10_items), "inline": False},
        {"name": "\U0001f4c5 財經日曆", "value": make_calendar_text(calendar_items), "inline": False},
        {"name": "\U0001f916 AI 分析（Claude）", "value": global_analysis, "inline": False},
        {"name": "\U0001f321 市場情緒", "value": market_sentiment, "inline": False},
        {"name": "\U0001f4f0 今日新聞（GPT-5翻譯）", "value": global_links, "inline": False}
    ]
})

print("完成！")
