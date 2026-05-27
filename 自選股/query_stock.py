"""
query_stock.py
由 GitHub Actions 觸發的即時查股腳本
透過 Discord Webhook 傳送 Claude AI 分析結果
"""

import os
import sys
import re
import time
import datetime
import requests
import feedparser
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from technical_indicators import get_full_indicators, format_indicators_for_prompt

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
DISCORD_WATCHLIST  = os.environ.get("DISCORD_WATCHLIST", "")
SYMBOL             = os.environ.get("STOCK_SYMBOL", "2330.TW").strip().upper()
NAME               = os.environ.get("STOCK_NAME", "").strip()

TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

# ── Claude AI ────────────────────────────────────────────────

def claude_call(prompt: str, max_tokens: int = 1200) -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        return f"AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        return f"AI 分析暫時無法使用（{str(e)[:100]}）"

# ── Yahoo Finance ────────────────────────────────────────────

def fetch_yahoo(symbol: str) -> dict:
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return None
        data   = r.json()
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        volumes= result["indicators"]["quote"][0].get("volume", [])
        meta   = result.get("meta", {})

        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg  = curr - prev
            pct  = chg / prev * 100
            vols = [v for v in volumes if v is not None]
            ma5  = sum(closes[-5:])  / min(5,  len(closes))
            ma10 = sum(closes[-10:]) / min(10, len(closes))
            return {
                "name":     meta.get("shortName", symbol),
                "price":    f"{curr:,.2f}",
                "change":   f"{chg:+.2f}",
                "pct":      f"{pct:+.2f}%",
                "emoji":    "🔴" if pct < 0 else "🟢",
                "currency": meta.get("currency", ""),
                "volume":   f"{vols[-1]:,.0f}" if vols else "N/A",
                "ma5":      f"{ma5:,.2f}",
                "ma10":     f"{ma10:,.2f}",
                "closes":   closes,
            }
        return None
    except Exception as e:
        print(f"Yahoo 錯誤：{e}")
        return None

# ── 新聞（多方查證：Google News + 鉅亨網 + Yahoo Finance）────

def fetch_news(symbol: str, name: str) -> str:
    titles = []
    seen   = set()

    def _add(url):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:6]:
                t = getattr(e, "title", "").strip()
                key = re.sub(r"\s+", "", t.lower())[:25]
                if t and key not in seen:
                    seen.add(key)
                    titles.append(t)
        except Exception:
            pass

    if ".TW" in symbol:
        stock_no = symbol.upper().replace(".TW", "")
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' 台股')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        time.sleep(0.2)
        _add("https://news.cnyes.com/rss/cat/tw_stock_news")
        time.sleep(0.2)
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(stock_no)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' stock earnings')}&hl=en-US&gl=US&ceid=US:en")
        time.sleep(0.2)
        _add(f"https://finance.yahoo.com/rss/headline?s={symbol}")
        time.sleep(0.2)
        _add("https://feeds.content.dowjones.io/public/rss/mw_topstories")

    return "\n".join(titles[:8]) if titles else "（暫無相關新聞）"

# ── 分析 ─────────────────────────────────────────────────────

def analyze(symbol: str, name: str, quote: dict, news: str, ind: dict = None) -> str:
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    indicators_text = format_indicators_for_prompt(ind) if ind else "（未取得技術指標）"

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析。

【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote['price']} {quote.get('currency', '')}
- 漲跌：{quote['change']} ({quote['pct']})
- {price_history}

【技術指標（實際計算值）】
{indicators_text}

【近期相關新聞（多方查證）】
{news}

請以繁體中文撰寫分析報告（900字以內），依據上方**實際技術指標數值**進行判斷：

📊 **近期走勢**（2-3句，說明目前價格與均線關係）

🔍 **技術面分析**
- 均線多空排列
- MACD 動能（依實際數值判斷）
- RSI 位置與超買超賣
- KDJ 訊號
- 乖離率（偏離程度）
- 成交量（量能配合度）
- 融資券動向（若有台股資料）
- 三大法人動向（若有台股資料）

📰 **基本面亮點**（結合近期新聞，2-3點）

🎯 **短期預測（1-2週）**
- 目標價區間：...
- 可能走向：...

💡 **操作建議**
- 多方進場條件：...
- 空方或觀望條件：...
- 停損參考：...

⚠️ 本報告由 AI 生成，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1400)

# ── Discord 傳送 ─────────────────────────────────────────────

def send(content: str):
    if not DISCORD_WATCHLIST:
        print("⚠️ 未設定 DISCORD_WATCHLIST")
        return
    chunks  = []
    current = ""
    for line in content.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        r = requests.post(DISCORD_WATCHLIST, json={"content": chunk}, timeout=15)
        if r.status_code in [200, 204]:
            print(f"✅ 傳送成功（{len(chunk)} 字）")
        else:
            print(f"❌ 傳送失敗：{r.status_code}")
        time.sleep(0.8)

# ── 主程式 ───────────────────────────────────────────────────

def main():
    print(f"🔍 查股：{SYMBOL} {NAME}")
    print(f"🕐 時間：{now_str()}")

    # 抓報價
    quote = fetch_yahoo(SYMBOL)
    if not quote:
        send(f"❌ 找不到股票代碼 `{SYMBOL}`\n請確認格式：\n• 台股：`2330.TW`\n• 美股：`NVDA`\n• 指數：`^TWII`")
        return

    display_name = NAME or quote.get("name", SYMBOL)
    print(f"✅ 報價：{quote['price']} ({quote['pct']})")

    # 抓新聞（多方查證）
    news = fetch_news(SYMBOL, display_name)
    time.sleep(0.3)

    # 技術指標
    print("📊 計算技術指標...")
    ind = get_full_indicators(SYMBOL)

    # AI 分析
    print("🤖 Claude 分析中...")
    analysis = analyze(SYMBOL, display_name, quote, news, ind)

    # 組合訊息
    header = (
        f"## {quote['emoji']} **{display_name}（{SYMBOL}）** 即時查股 | {now_str()}\n"
        f"💰 **{quote['price']}** {quote.get('currency','')} "
        f"{quote['change']} ({quote['pct']})\n\n"
    )

    send(header + analysis)
    print("✅ 完成！")

if __name__ == "__main__":
    main()
