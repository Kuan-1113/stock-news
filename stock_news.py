import requests
import os
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

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
    return [a for a in articles if a.get("title") and "[Removed]" not in a.get("title","")]

def fetch_market_data():
    symbols = {
        "台灣加權": "^TWII",
        "道瓊": "^DJI",
        "納斯達克": "^IXIC",
        "S&P500": "^GSPC",
        "黃金": "GC=F",
        "原油": "CL=F",
        "美債20年(TLT)": "TLT",
        "美元指數": "DX-Y.NYB"
    }
    results = {}
    for name, symbol in symbols.items():
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2d"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=10)
            data = r.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                prev, curr = closes[-2], closes[-1]
                change = curr - prev
                pct = (change / prev) * 100
                arrow = "🔴" if change < 0 else "🟢"
                results[name] = f"{arrow} {curr:,.2f} ({pct:+.2f}%)"
            else:
                results[name] = "數據不足"
        except:
            results[name] = "無法取得"
    return results

def analyze(articles, category):
    if not articles:
        return "今日暫無相關新聞。"
    titles_with_source = "\n".join([
        f"{i+1}. {a['title']} ({a.get('source',{}).get('name','')})"
        for i, a in enumerate(articles)
    ])
    prompt = (
        "以下是今日" + category + "的新聞標題列表，請用繁體中文完整回應以下四點：\n\n"
        "【一、逐則中文翻譯】\n請將每一則標題翻譯成繁體中文（編號對應）\n\n"
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
        return "AI分析暫時無法使用：" + str(result.get("error", {}).get("message", ""))

def news_to_html(articles):
    if not articles:
        return "<p style='color:#aaa'>今日暫無相關新聞</p>"
    html = ""
    for a in articles:
        title = a.get("title", "")
        url = clean_url(a.get("url", "#"))
        source = a.get("source", {}).get("name", "")
        html += (
            '<div style="margin-bottom:10px;padding:10px;background:#f9f9f9;'
            'border-left:3px solid #e94560;border-radius:4px">'
            f'<a href="{url}" style="color:#1a1a2e;font-weight:bold;text-decoration:none">{title}</a>'
            f'<div style="font-size:12px;color:#888;margin-top:4px">{source}'
            f' &nbsp;|&nbsp; <a href="{url}" style="color:#e94560">閱讀原文 →</a></div>'
            '</div>'
        )
    return html

def analysis_to_html(text):
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = text.strip().split("\n")
    html = "<ul style='padding-left:20px;margin:8px 0'>" if False else ""
    in_list = False
    for line in lines:
        line = line.strip()
        if not line:
            if in_list:
                html += "</ul>"
                in_list = False
            html += "<br>"
        elif line.startswith("【") or line.startswith("##"):
            if in_list:
                html += "</ul>"
                in_list = False
            clean = re.sub(r'[#【】]', '', line).strip()
            html += f'<h3 style="color:#1a1a2e;margin:16px 0 8px;font-size:15px">▍{clean}</h3>'
        elif re.match(r'^[\-\*\•]', line):
            if not in_list:
                html += '<ul style="padding-left:20px;margin:6px 0">'
                in_list = True
            clean = re.sub(r'^[\-\*\•]\s*', '', line)
            html += f'<li style="margin-bottom:5px;line-height:1.7">{clean}</li>'
        elif re.match(r'^\d+\.', line):
            if in_list:
                html += "</ul>"
                in_list = False
            html += f'<p style="margin:5px 0;line-height:1.8">{line}</p>'
        else:
            if in_list:
                html += "</ul>"
                in_list = False
            html += f'<p style="margin:5px 0;line-height:1.8">{line}</p>'
    if in_list:
        html += "</ul>"
    return html

def market_to_html(data):
    html = '<table style="width:100%;border-collapse:collapse">'
    items = list(data.items())
    for i in range(0, len(items), 2):
        html += '<tr>'
        for j in range(2):
            if i + j < len(items):
                name, val = items[i+j]
                html += (
                    '<td style="padding:8px;border-bottom:1px solid #eee;width:50%">'
                    f'<span style="color
