import requests
import os
import time
from datetime import date

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

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

def fetch_news(query, lang, count=5):
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": lang,
            "sortBy": "publishedAt",
            "pageSize": count,
            "apiKey": NEWS_API_KEY
        }
        res = requests.get(url, params=params, timeout=10)
        articles = res.json().get("articles", [])
        return [a for a in articles if a.get("title") and "[Removed]" not in a.get("title", "")]
    except Exception:
        return []

def analyze(articles, category):
    if not articles:
        return "今日暫無相關新聞。"
    titles = "\n".join([str(i+1) + ". " + a["title"] for i, a in enumerate(articles)])
    prompt = (
        "以下是今日" + category + "新聞，請用繁體中文寫2-3個重點，每點用•開頭，總字數不超過120字，不要用###標題：\n\n" + titles
    )
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
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = res.json()
        if "content" in result:
            return result["content"][0]["text"].strip()[:800]
        return "AI分析暫時無法使用"
    except Exception:
        return "AI分析暫時無法使用"

def analyze_global(vix, us10y, dxy, btc, eth, sol):
    prompt = (
        "請用繁體中文一句話（不超過60字）分析以下指標對今日市場情緒的影響，不要標題：\n"
        "VIX:" + vix + " 美債:" + us10y + " DXY:" + dxy + " BTC:" + btc + " ETH:" + eth + " SOL:" + sol
    )
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
                "max_tokens": 150,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        result = res.json()
        if "content" in result:
            return result["content"][0]["text"].strip()[:300]
        return "AI分析暫時無法使用"
    except Exception:
        return "AI分析暫時無法使用"

def make_news_links(articles):
    if not articles:
        return "暫無新聞連結"
    lines = []
    for i, a in enumerate(articles[:5]):
        title = a.get("title", "")[:35] + "..."
        url = a.get("url", "#")
        lines.append(str(i+1) + ". [" + title + "](" + url + ")")
    return "\n".join(lines)

def send_embed(embed):
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        if res.status_code in [200, 204]:
            print("發送成功：" + embed.get("title", ""))
        else:
            print("發送失敗：" + str(res.status_code) + " " + res.text[:200])
    except Exception as e:
        print("發送錯誤：" + str(e))
    time.sleep(1)

today = date.today().strftime("%Y-%m-%d")
print("開始執行...")

print("抓取大盤數據...")
twii = fetch_yahoo("^TWII")
dji = fetch_yahoo("^DJI")
ixic = fetch_yahoo("^IXIC")
gspc = fetch_yahoo("^GSPC")
vix = fetch_yahoo("^VIX")
us10y = fetch_yahoo("^TNX")
dxy = fetch_yahoo("DX-Y.NYB")
gold = fetch_yahoo("GC=F")
oil = fetch_yahoo("CL=F")

print("抓取加密貨幣...")
btc = fetch_crypto("BTCUSDT")
eth = fetch_crypto("ETHUSDT")
sol = fetch_crypto("SOLUSDT")

print("抓取台股籌碼...")
flows = fetch_twse_flows()

print("抓取新聞...")
tw_articles = fetch_news("Taiwan stock market TWSE economy", "zh", 5)
us_articles = fetch_news("US stock market Wall Street NYSE NASDAQ", "en", 5)
global_articles = fetch_news("war economy geopolitics oil Fed interest rate", "en", 5)

print("AI 分析中...")
tw_analysis = analyze(tw_articles, "台股")
us_analysis = analyze(us_articles, "美股")
global_insight = analyze_global(vix, us10y, dxy, btc, eth, sol)

if flows:
    flows_text = "外資：" + flows["外資"] + "\n投信：" + flows["投信"] + "\n自營商：" + flows["自營商"]
else:
    flows_text = "今日籌碼資料暫無法取得"

print("發送到 Discord...")

send_embed({
    "title": "\U0001f1f9\U0001f1fc 台股市場 | " + today,
    "color": 3066993,
    "fields": [
        {"name": "\U0001f4ca 大盤", "value": "台灣加權：" + twii, "inline": False},
        {"name": "\U0001f3e6 法人籌碼", "value": flows_text, "inline": False},
        {"name": "\U0001f916 AI 分析", "value": tw_analysis, "inline": False},
        {"name": "\U0001f4f0 新聞連結", "value": make_news_links(tw_articles), "inline": False}
    ]
})

send_embed({
    "title": "\U0001f1fa\U0001f1f8 美股市場 | " + today,
    "color": 3447003,
    "fields": [
        {"name": "\U0001f4ca 大盤", "value": "道瓊：" + dji + "\n納斯達克：" + ixic + "\nS&P500：" + gspc, "inline": False},
        {"name": "\U0001f916 AI 分析", "value": us_analysis, "inline": False},
        {"name": "\U0001f4f0 新聞連結", "value": make_news_links(us_articles), "inline": False}
    ]
})

send_embed({
    "title": "\U0001f310 全球市場溫度計 | " + today,
    "color": 10181046,
    "fields": [
        {"name": "\U0001f4c9 總體指標", "value": "VIX：" + vix + "\n美債10Y：" + us10y + "\nDXY：" + dxy + "\n黃金：" + gold + "\n原油：" + oil, "inline": True},
        {"name": "\U0001fa99 加密貨幣", "value": "BTC：" + btc + "\nETH：" + eth + "\nSOL：" + sol, "inline": True},
        {"name": "\U0001f916 AI 情緒分析", "value": global_insight, "inline": False}
    ]
})

print("完成！")
