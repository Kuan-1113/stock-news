import requests
import os
import smtplib
from email.mime.text import MIMEText
from datetime import date

NEWS_API_KEY = os.environ["NEWS_API_KEY"]
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

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
    result = ""
    for a in articles:
        result += f"• {a['title']}\n  {a['url']}\n\n"
    return result

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

report = f"""📅 {today} 股市日報

🇹🇼 台股新聞：
{tw_news}
🇺🇸 美股新聞：
{us_news}
"""

save_file(report)
send_email(f"📈 {today} 股市日報", report)
print("完成！")
