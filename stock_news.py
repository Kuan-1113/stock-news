import requests
import os
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date
from urllib.parse import urlparse, urlunparse

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

def fetch_news(query, lang, count=15):
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": lang,
        "sortBy": "publishedAt",
        "pageSize": count,
        "apiKey": NEWS_API_KEY
    }
    res = requests.get(url, params=params)
    articles = res.json().get("articles", [])
    return [a for a in articles if a.get("title") and "[Removed]" not in a.get("title", "")]

def fetch_market_data():
    symbols = {
        "taiwan": "^TWII",
        "dow": "^DJI",
        "nasdaq": "^IXIC",
        "sp500": "^GSPC",
        "gold": "GC=F",
        "oil": "CL=F",
        "tlt": "TLT",
        "usd": "DX-Y.NYB"
    }
    names = {
        "taiwan": "台灣加權",
        "dow": "道瓊",
        "nasdaq": "納斯達克",
        "sp500": "S&P500",
        "gold": "黃金",
        "oil": "原油",
        "tlt": "美債20年(TLT)",
        "usd": "美元指數"
    }
    results = {}
    for key, symbol in symbols.items():
        try:
            api_url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?interval=1d&range=2d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(api_url, headers=headers, timeout=10)
            data = r.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                prev, curr = closes[-2], closes[-1]
                change = curr - prev
                pct = (change / prev) * 100
                arrow = "🔴" if change < 0 else "🟢"
                results[names[key]] = arrow + " " + "{:,.2f}".format(curr) + " (" + "{:+.2f}".format(pct) + "%)"
            else:
                results[names[key]] = "數據不足"
        except Exception:
            results[names[key]] = "無法取得"
    return results

def analyze(articles, category):
    if not articles:
        return "今日暫無相關新聞。"
    titles_with_source = "\n".join([
        str(i+1) + ". " + a["title"] + " (" + a.get("source", {}).get("name", "") + ")"
        for i, a in enumerate(articles)
    ])
    prompt = (
        "以下是今日" + category + "的新聞標題列表，請用繁體中文完整回應以下四點：\n\n"
        "【一、逐則中文翻譯】\n請將每一則標題翻譯成繁體中文\n\n"
        "【二、整體趨勢分析】\n分析這些新聞反映的整體市場趨勢\n\n"
        "【三、受影響產業與股票】\n說明可能受到影響的產業、類股或個股\n\n"
        "【四、投資人重點提醒】\n給投資人一句最重要的操作提醒\n\n"
        "新聞列表：\n" + titles_with_source
    )
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]
        }
    )
    result = res.json()
    if "content" in result:
        return result["content"][0]["text"]
    else:
        return "AI分析暫時無法使用"
def news_to_html(articles):
    if not articles:
        return "<p style='color:#aaa'>今日暫無相關新聞</p>"
    html = ""
    for a in articles:
        title = a.get("title", "")
        url = clean_url(a.get("url", "#"))
        source = a.get("source", {}).get("name", "")
        html += (
            "<div style='margin-bottom:10px;padding:10px;background:#f9f9f9;"
            "border-left:3px solid #e94560;border-radius:4px'>"
            "<a href='" + url + "' style='color:#1a1a2e;font-weight:bold;text-decoration:none'>" + title + "</a>"
            "<div style='font-size:12px;color:#888;margin-top:4px'>" + source +
            " &nbsp;|&nbsp; <a href='" + url + "' style='color:#e94560'>閱讀原文</a></div>"
            "</div>"
        )
    return html

def analysis_to_html(text):
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = text.strip().split("\n")
    html = ""
    in_list = False
    for line in lines:
        line = line.strip()
        if not line:
            if in_list:
                html += "</ul>"
                in_list = False
            html += "<br>"
        elif line.startswith("【"):
            if in_list:
                html += "</ul>"
                in_list = False
            clean = re.sub(r'[【】]', '', line).strip()
            html += "<h3 style='color:#1a1a2e;margin:16px 0 8px;font-size:15px'>▍" + clean + "</h3>"
        elif re.match(r'^[\-\*]', line):
            if not in_list:
                html += "<ul style='padding-left:20px;margin:6px 0'>"
                in_list = True
            clean = re.sub(r'^[\-\*]\s*', '', line)
            html += "<li style='margin-bottom:5px;line-height:1.7'>" + clean + "</li>"
        else:
            if in_list:
                html += "</ul>"
                in_list = False
            html += "<p style='margin:5px 0;line-height:1.8'>" + line + "</p>"
    if in_list:
        html += "</ul>"
    return html

def market_to_html(data):
    html = "<table style='width:100%;border-collapse:collapse'>"
    items = list(data.items())
    for i in range(0, len(items), 2):
        html += "<tr>"
        for j in range(2):
            if i + j < len(items):
                name, val = items[i+j]
                html += (
                    "<td style='padding:8px;border-bottom:1px solid #eee;width:50%'>"
                    "<span style='color:#666;font-size:13px'>" + name + "</span><br>"
                    "<span style='font-size:14px;font-weight:bold'>" + val + "</span>"
                    "</td>"
                )
        html += "</tr>"
    html += "</table>"
    return html

def build_html(today, market, tw_a, us_a, gl_a, tw_ana, us_ana, gl_ana):
    return (
        "<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px;color:#333;background:#f5f5f5'>"
        "<div style='background:#1a1a2e;color:white;padding:24px;border-radius:10px;margin-bottom:20px'>"
        "<h1 style='margin:0;font-size:26px'>📈 股市日報</h1>"
        "<p style='margin:6px 0 0;opacity:0.6;font-size:14px'>" + today + " ・ 自動生成報告</p>"
        "</div>"
        "<div style='background:white;border-radius:10px;padding:20px;margin-bottom:16px'>"
        "<h2 style='color:#1a1a2e;margin:0 0 16px;font-size:17px'>📊 今日大盤與商品</h2>"
        + market_to_html(market) +
        "</div>"
        "<div style='background:white;border-radius:10px;padding:20px;margin-bottom:16px'>"
        "<h2 style='color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px;font-size:17px'>🇹🇼 台股新聞</h2>"
        + news_to_html(tw_a) +
        "<div style='background:#f0f4ff;padding:16px;border-radius:8px;margin-top:16px'>"
        "<h3 style='margin:0 0 10px;color:#1a1a2e;font-size:15px'>🤖 AI 分析</h3>"
        + analysis_to_html(tw_ana) +
        "</div></div>"
        "<div style='background:white;border-radius:10px;padding:20px;margin-bottom:16px'>"
        "<h2 style='color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px;font-size:17px'>🇺🇸 美股新聞</h2>"
        + news_to_html(us_a) +
        "<div style='background:#f0f4ff;padding:16px;border-radius:8px;margin-top:16px'>"
        "<h3 style='margin:0 0 10px;color:#1a1a2e;font-size:15px'>🤖 AI 分析</h3>"
        + analysis_to_html(us_ana) +
        "</div></div>"
        "<div style='background:white;border-radius:10px;padding:20px;margin-bottom:16px'>"
        "<h2 style='color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px;font-size:17px'>🌍 全球局勢</h2>"
        + news_to_html(gl_a) +
        "<div style='background:#f0f4ff;padding:16px;border-radius:8px;margin-top:16px'>"
        "<h3 style='margin:0 0 10px;color:#1a1a2e;font-size:15px'>🤖 AI 分析</h3>"
        + analysis_to_html(gl_ana) +
        "</div></div>"
        "<p style='color:#aaa;font-size:12px;text-align:center'>此報告由 AI 自動生成，僅供參考，不構成投資建議。</p>"
        "</body></html>"
    )

def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)

def save_file(content, label):
    today = date.today().strftime("%Y-%m-%d")
    os.makedirs("reports", exist_ok=True)
    with open("reports/" + today + "-" + label + ".txt", "w", encoding="utf-8") as f:
        f.write(content)

today = date.today().strftime("%Y-%m-%d")
print("抓取大盤數據...")
market = fetch_market_data()
print("抓取新聞...")
tw_articles = fetch_news("Taiwan stock market TWSE economy", "zh", 15)
us_articles = fetch_news("US stock market Wall Street NYSE NASDAQ", "en", 15)
global_articles = fetch_news("war economy geopolitics oil Fed interest rate", "en", 15)
print("AI 分析中...")
tw_analysis = analyze(tw_articles, "台股")
us_analysis = analyze(us_articles, "美股")
global_analysis = analyze(global_articles, "全球局勢")
html = build_html(today, market, tw_articles, us_articles, global_articles, tw_analysis, us_analysis, global_analysis)
save_file(tw_analysis + "\n" + us_analysis + "\n" + global_analysis, "daily")
send_email("📈 " + today + " 股市日報", html)
print("完成！")        
