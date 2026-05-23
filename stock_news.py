import requests
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

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
    return articles

def analyze(articles, category):
    titles = "\n".join([f"- {a['title']}" for a in articles])
    prompt = "以下是今日" + category + "新聞標題，請用繁體中文回應：\n1. 逐則翻譯標題\n2. 分析整體趨勢\n3. 說明可能影響哪些產業或股票\n4. 給投資人一句重點提醒\n\n新聞：\n" + titles
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
    html = ""
    for a in articles:
        title = a.get("title", "")
        url = a.get("url", "#")
        source = a.get("source", {}).get("name", "")
        html += f'<div style="margin-bottom:12px;padding:10px;background:#f9f9f9;border-left:3px solid #e94560;border-radius:4px">'
        html += f'<a href="{url}" style="color:#1a1a2e;font-weight:bold;text-decoration:none">{title}</a>'
        html += f'<div style="font-size:12px;color:#888;margin-top:4px">{source} &nbsp;|&nbsp; <a href="{url}" style="color:#e94560">閱讀原文</a></div>'
        html += '</div>'
    return html

def analysis_to_html(text):
    lines = text.strip().split("\n")
    html = ""
    for line in lines:
        line = line.strip()
        if not line:
            html += "<br>"
        elif line.startswith("##"):
            html += f'<h3 style="color:#1a1a2e;margin:16px 0 8px">{line.replace("##","").strip()}</h3>'
        elif line.startswith("#"):
            html += f'<h2 style="color:#e94560;margin:20px 0 10px">{line.replace("#","").strip()}</h2>'
        elif line.startswith("-") or line.startswith("*"):
            html += f'<li style="margin-bottom:6px">{line[1:].strip()}</li>'
        else:
            html += f'<p style="margin:6px 0;line-height:1.8">{line}</p>'
    return html

def build_html(today, tw_articles, us_articles, global_articles, tw_analysis, us_analysis, global_analysis):
    return f"""
<html>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px;color:#333">

<div style="background:#1a1a2e;color:white;padding:20px;border-radius:8px;margin-bottom:24px">
  <h1 style="margin:0;font-size:24px">📈 股市日報</h1>
  <p style="margin:6px 0 0;opacity:0.7">{today}</p>
</div>

<div style="background:white;border:1px solid #eee;border-radius:8px;padding:20px;margin-bottom:20px">
  <h2 style="color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px">🇹🇼 台股新聞</h2>
  {news_to_html(tw_articles)}
  <div style="background:#f0f4ff;padding:16px;border-radius:6px;margin-top:16px">
    <h3 style="margin:0 0 10px;color:#1a1a2e">🤖 AI 分析</h3>
    {analysis_to_html(tw_analysis)}
  </div>
</div>

<div style="background:white;border:1px solid #eee;border-radius:8px;padding:20px;margin-bottom:20px">
  <h2 style="color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px">🇺🇸 美股新聞</h2>
  {news_to_html(us_articles)}
  <div style="background:#f0f4ff;padding:16px;border-radius:6px;margin-top:16px">
    <h3 style="margin:0 0 10px;color:#1a1a2e">🤖 AI 分析</h3>
    {analysis_to_html(us_analysis)}
  </div>
</div>

<div style="background:white;border:1px solid #eee;border-radius:8px;padding:20px;margin-bottom:20px">
  <h2 style="color:#e94560;border-bottom:2px solid #e94560;padding-bottom:8px">🌍 全球局勢</h2>
  {news_to_html(global_articles)}
  <div style="background:#f0f4ff;padding:16px;border-radius:6px;margin-top:16px">
    <h3 style="margin:0 0 10px;color:#1a1a2e">🤖 AI 分析</h3>
    {analysis_to_html(global_analysis)}
  </div>
</div>

<p style="color:#aaa;font-size:12px;text-align:center">此報告由 AI 自動生成，僅供參考，不構成投資建議。</p>
</body>
</html>
"""

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

tw_articles = fetch_news("Taiwan stock market economy", "zh", 15)
us_articles = fetch_news("US stock market Wall Street", "en", 15)
global_articles = fetch_news("war economy geopolitics oil energy", "en", 15)

tw_analysis = analyze(tw_articles, "台股")
us_analysis = analyze(us_articles, "美股")
global_analysis = analyze(global_articles, "全球局勢")

html = build_html(today, tw_articles, us_articles, global_articles, tw_analysis, us_analysis, global_analysis)

save_file(tw_analysis + us_analysis + global_analysis, "daily")
send_email("📈 " + today + " 股市日報", html)
print("完成！")
