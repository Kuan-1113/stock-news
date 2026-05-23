import requests
import os
import smtplib
from email.mime.text import MIMEText
from datetime import date

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

def fetch_news(query, lang):
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": lang,
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": NEWS_API_KEY
    }
    res = requests.get(url, params=params)
    articles = res.json().get("articles", [])
    return "\n".join([f"- {a['title']}" for a in articles])

def summarize(text):
    res = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": f"請用繁體中文條列摘要以下股市新聞，每則一行：\n{text}"}]
        }
    )
    return res.json()["content"][0]["text"]

def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)

def save_file(content):
    today = date.today().strftime("%Y-%m-%d")
    os.makedirs("reports", exist_ok=True)
    with open(f"reports/{today}.txt", "w", encoding="utf-8") as f:
        f.write(content)

# 主流程
today = date.today().strftime("%Y-%m-%d")
tw_news = fetch_news("台股 股市", "zh")
us_news = fetch_news("US stock market", "en")

tw_summary = summarize(tw_news)
us_summary = summarize(us_news)

report = f"""📅 {today} 股市日報

🇹🇼 台股摘要：
{tw_summary}

🇺🇸 美股摘要：
{us_summary}
"""

save_file(report)
send_email(f"📈 {today} 股市日報", report)
print("完成！")
