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
    prompt = f"""以下是今日{category}新聞標題，請用繁體中文：
1. 翻譯每則標題
2. 分析整體趨勢
3. 說明可能影響哪些產業或股票
4. 給投資人一句重點提醒

新聞：
{titles}"""

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
    print("API回應：", result)
    if "content" in result:
        return result["content"][0]["text"]
    else:
        error_msg = result.get("error", {}).get("message", "未知錯誤")
        return f"AI分析暫時無法使用：{error_msg}"

def send_email(subject, body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_ADDRESS

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px">
    <h1 style="color:#1a1a2e;border-bottom:3px solid #e94560;padding-bottom:10px">
    📈 股市日報</h1>
    <pre style="white-space:pre-wrap;font-size:14px;line-height:1.8">{body}</pre>
    <hr style="margin-top:30px">
    <p style="color:#888;font-size:12px">此報告由 AI 自動生成，僅供參考，不構成投資建議。</p>
    </body></html>
    """
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)

def save_file(content, label):
    today = date.today().strftime("%Y-%m-%d")
    os.makedirs("reports", exist_ok=True)
    with open(f"reports/{today}-{label}.txt", "w", encoding="utf-8") as f:
        f.write(content)

# 主流程
today = date.today().strftime("%Y-%m-%d")

tw_articles = fetch_news("台股 股市 台灣經濟
