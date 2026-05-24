
import requests
from bs4 import BeautifulSoup
from google import genai
import os
import datetime

# Gemini API Key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Discord Webhook URL
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# 如果沒有設定 API Key，則跳過 Gemini API 呼叫
if not GEMINI_API_KEY:
    print("警告：未設定 GEMINI_API_KEY 環境變數，將跳過 Gemini 分析。")
    gemini_client = None
else:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def fetch_news(url):
    """從指定網址抓取新聞標題和內容"""
    try:
        response = requests.get(url, verify=False)  # 禁用SSL憑證驗證
        response.raise_for_status()  # 檢查請求是否成功
        soup = BeautifulSoup(response.text, "html.parser")

        news_articles = []

        if "moneydj.com" in url:
            title_tag = soup.find("h1")  # 假設新聞標題在 h1 標籤
            content_tags = soup.find_all("p")  # 假設新聞內容在多個 p 標籤

            title = title_tag.get_text(strip=True) if title_tag else "無標題"
            content = " ".join([p.get_text(strip=True) for p in content_tags]) if content_tags else "無內容"
            news_articles.append({"title": title, "content": content})
        else:
            articles = soup.find_all("div", class_="news-article")

            for article in articles:
                title_tag = article.find("h3")
                content_tag = article.find("p")
                if title_tag and content_tag:
                    title = title_tag.get_text(strip=True)
                    content = content_tag.get_text(strip=True)
                    news_articles.append({"title": title, "content": content})

        return news_articles
    except requests.exceptions.RequestException as e:
        print(f"新聞抓取失敗：{e}")
        return []

def analyze_with_gemini(news_data):
    """使用 Gemini 分析新聞並生成日報內容"""
    prompt_template = """
你是一位資深的Python專家與資深台股分析師。請根據以下台股重大焦點新聞，分析當天大盤的均線支撐（如月線、季線等技術面）與RSI/MACD動態，並針對個股的利多/利空給出具體的專業解讀。請多利用表格（Table）來呈現數據與重點，個股名稱與代號用粗體標記，最後輸出成Markdown格式的日報。

新聞內容：
---
{}
---

請產生當天的台股股市日報：
"""

    news_text = "\n\n".join([f"標題：{n['title']}\n內容：{n['content']}" for n in news_data])
    prompt = prompt_template.format(news_text)

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        print(f"Gemini API 呼叫失敗：{e}")
        return "無法生成日報內容。"

def save_report(report_content, filename="daily_report.md"):
    """將日報內容儲存為Markdown檔案"""
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(report_content)
        print(f"日報已成功儲存至 {filename}")
    except IOError as e:
        print(f"日報儲存失敗：{e}")

def send_to_discord(report_content):
    """將日報內容傳送至 Discord Webhook"""
    if not DISCORD_WEBHOOK_URL:
        print("警告：未設定 DISCORD_WEBHOOK_URL 環境變數，將跳過 Discord 傳送。")
        return

    # Discord 訊息有 2000 字元限制，超過需分段傳送
    max_length = 1900
    chunks = [report_content[i:i+max_length] for i in range(0, len(report_content), max_length)]

    try:
        for idx, chunk in enumerate(chunks):
            payload = {"content": chunk}
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            print(f"Discord 傳送成功（第 {idx+1}/{len(chunks)} 段）")
    except requests.exceptions.RequestException as e:
        print(f"Discord 傳送失敗：{e}")


if __name__ == "__main__":
    # 範例新聞來源 (請替換成實際的台股新聞網站)
    NEWS_URL = "https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=mb010000213165"

    print(f"正在從 {NEWS_URL} 抓取新聞...")
    news_articles = fetch_news(NEWS_URL)

    if news_articles:
        if gemini_client:
            print("新聞抓取成功，正在使用 Gemini 分析並生成日報...")
            daily_report = analyze_with_gemini(news_articles)
            save_report(daily_report)
            send_to_discord(daily_report)
        else:
            print("未設定 Gemini API Key，跳過 Gemini 分析。將生成一個包含新聞標題的簡單報告。")
            simple_report_content = "# 今日台股重大焦點新聞\n\n"
            for i, news in enumerate(news_articles):
                simple_report_content += f"## {i+1}. {news['title']}\n"
                simple_report_content += f"{news['content']}\n\n"
            save_report(simple_report_content)
            send_to_discord(simple_report_content)
    else:
        print("沒有抓取到新聞，無法生成日報。")
