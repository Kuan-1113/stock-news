"""
stock_daily.py
台股 / 美股 / 國際新聞爬蟲 + Claude AI 分析 + Discord 傳送
使用免費 RSS（Yahoo Finance / Google News / MoneyDJ / 鉅亨網）

定時規則（台灣時間）：
  08:00 → 整理前天 22:00 ~ 當天 08:00 的新聞
  14:00 → 整理當天 08:00 ~ 14:00 的新聞
  22:00 → 整理當天 14:00 ~ 22:00 的新聞

週六日：新聞照常整理，指標數據若無更新則標註「未更新（上次交易日數據）」

使用方式：
  1. 設定環境變數 ANTHROPIC_API_KEY
  2. python stock_daily.py          ← 立即執行一次（測試用）
  3. python stock_daily.py --schedule ← 啟動排程（每日 08:00 / 14:00 / 22:00）
"""

import os
import sys
import re
import time
import datetime
import warnings
import feedparser
import requests
import schedule
import anthropic

# 強制 stdout/stderr 使用 UTF-8（解決 Windows cp950 emoji 問題）
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────
# 設定區
# ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DISCORD_TW     = "https://discord.com/api/webhooks/1507952802662449152/8iumIv-Bs5PTRVlMpFXbE7wH_uzHJlLtmybTHaj1zUDxksQBZwRAOs7v69tvSOezmWnW"
DISCORD_US     = "https://discord.com/api/webhooks/1507952945130635307/3Rd1BBhGElvH4N7RZeQaOHNp5FqiCGBO4d9UZwK29dY1wksN70CWYH4MJ19tRfUuSOVX"
DISCORD_GLOBAL = "https://discord.com/api/webhooks/1507953174512668902/QsKOUt5afzwQYfbQQeGi8Tza2-gkLKUJaP-B03lWEyX9C5ops59NuGHLJCK7a8UC9N5-"
DISCORD_WATCHLIST = "https://discord.com/api/webhooks/1508115009924894744/rlYl9lqindxWlauA4Ie4UJsWbneXM2nQZbuCa8_0XWKLvF3gpku5LONetISQ-MHzJhl1"

TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 自選股清單
WATCHLIST = [
    {"symbol": "2330.TW", "name": "台積電"},
    {"symbol": "2454.TW", "name": "聯發科"},
    {"symbol": "NVDA",    "name": "輝達"},
    {"symbol": "AAPL",    "name": "蘋果"},
    {"symbol": "TSLA",    "name": "特斯拉"},
    {"symbol": "^TWII",   "name": "加權指數"},
]

# ─────────────────────────────────────────────────────────────
# 時段判斷
# ─────────────────────────────────────────────────────────────
def get_session_info() -> dict:
    """依目前台灣時間決定本次時段資訊"""
    h = datetime.datetime.now(TW_TZ).hour
    if 8 <= h < 14:
        return {
            "label": "盤前早報",
            "period": "前日 22:00 ～ 今日 08:00",
            "emoji": "🌅",
            "start_h": 22,
            "end_h": 8,
        }
    elif 14 <= h < 22:
        return {
            "label": "盤中午報",
            "period": "今日 08:00 ～ 14:00",
            "emoji": "☀️",
            "start_h": 8,
            "end_h": 14,
        }
    else:
        return {
            "label": "盤後晚報",
            "period": "今日 14:00 ～ 22:00",
            "emoji": "🌙",
            "start_h": 14,
            "end_h": 22,
        }

def is_weekend() -> bool:
    """判斷今天是否為週六日"""
    return datetime.datetime.now(TW_TZ).weekday() >= 5  # 5=Saturday, 6=Sunday

def now_tw() -> datetime.datetime:
    return datetime.datetime.now(TW_TZ)

def now_str() -> str:
    return now_tw().strftime("%Y-%m-%d %H:%M")

# ─────────────────────────────────────────────────────────────
# Claude AI 客戶端
# ─────────────────────────────────────────────────────────────
def get_claude_client():
    if not ANTHROPIC_API_KEY:
        return None
    try:
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as e:
        print(f"❌ Claude 客戶端初始化失敗：{e}")
        return None

claude_client = get_claude_client()

def claude_call(prompt: str, max_tokens: int = 1500) -> str:
    """呼叫 Claude API"""
    if not claude_client:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"
    try:
        message = claude_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"AI 分析暫時無法使用（{str(e)[:100]}）"

# ─────────────────────────────────────────────────────────────
# 大盤數據（Yahoo Finance）
# ─────────────────────────────────────────────────────────────
def fetch_yahoo(symbol: str, name: str = "") -> dict:
    """抓取 Yahoo Finance 報價，回傳 dict"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        timestamps = result.get("timestamp", [])
        closes = [c for c in closes if c is not None]
        meta = result.get("meta", {})
        currency = meta.get("currency", "")

        # 判斷數據是否為舊數據（週末）
        stale = False
        if timestamps and is_weekend():
            last_ts = timestamps[-1]
            last_dt = datetime.datetime.fromtimestamp(last_ts, tz=TW_TZ)
            if last_dt.weekday() >= 5:
                stale = True
            elif (now_tw() - last_dt).days >= 1:
                stale = True

        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg = curr - prev
            pct = chg / prev * 100
            emoji = "🔴" if pct < 0 else "🟢"
            return {
                "name": name or symbol,
                "price": f"{curr:,.2f}",
                "change": f"{chg:+.2f}",
                "pct": f"{pct:+.2f}%",
                "emoji": emoji,
                "currency": currency,
                "stale": stale,
            }
        return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}
    except Exception as e:
        print(f"Yahoo 錯誤 {symbol}：{e}")
        return {"name": name or symbol, "price": "N/A", "change": "N/A", "pct": "N/A", "emoji": "⚪", "stale": False}

def fmt_quote(q: dict) -> str:
    stale_tag = " ⚠️未更新（上次交易日）" if q.get("stale") else ""
    return f"{q['emoji']} {q['price']} ({q['pct']}){stale_tag}"

# ─────────────────────────────────────────────────────────────
# 加密貨幣（CoinGecko 免費 API）
# ─────────────────────────────────────────────────────────────
def fetch_crypto() -> dict:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        results = {}
        for symbol, cg_id in {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.items():
            if cg_id in data:
                price = data[cg_id]["usd"]
                pct = data[cg_id].get("usd_24h_change", 0) or 0
                emoji = "🔴" if pct < 0 else "🟢"
                results[symbol] = {"price": f"${price:,.2f}", "pct": f"{pct:+.2f}%", "emoji": emoji}
        return results
    except Exception as e:
        print(f"CoinGecko 錯誤：{e}")
        return {}

# ─────────────────────────────────────────────────────────────
# RSS 新聞爬蟲（免費，無需 API Key）
# ─────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "tw": [
        "https://news.cnyes.com/rss/cat/tw_stock",
        "https://tw.stock.yahoo.com/rss",
        "https://news.google.com/rss/search?q=台股+股市&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://news.google.com/rss/search?q=台灣+財經+科技股&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "https://www.moneydj.com/rss/news.aspx?svc=NW&cat=MB",
    ],
    "us": [
        "https://news.google.com/rss/search?q=US+stock+market+Wall+Street&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=NASDAQ+S%26P500+Fed+earnings&hl=en-US&gl=US&ceid=US:en",
        "https://finance.yahoo.com/rss/topstories",
        "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    ],
    "global": [
        "https://news.google.com/rss/search?q=global+economy+Fed+inflation+oil&hl=en-US&gl=US&ceid=US:en",
        "https://news.google.com/rss/search?q=geopolitics+trade+war+OPEC&hl=en-US&gl=US&ceid=US:en",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ],
}

def parse_rss_date(entry) -> datetime.datetime | None:
    for attr in ["published_parsed", "updated_parsed"]:
        t = getattr(entry, attr, None)
        if t:
            try:
                dt = datetime.datetime(*t[:6], tzinfo=datetime.timezone.utc)
                return dt.astimezone(TW_TZ)
            except Exception:
                pass
    return None

def fetch_rss(url: str, limit: int = 10) -> list:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        feed = feedparser.parse(url, request_headers=headers)
        articles = []
        for entry in feed.entries[:limit]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "#").strip()
            summary = getattr(entry, "summary", "").strip()
            summary = re.sub(r"<[^>]+>", "", summary)[:200]
            pub_dt = parse_rss_date(entry)
            if title and len(title) > 5:
                articles.append({
                    "title": title,
                    "url": link,
                    "summary": summary,
                    "pub_dt": pub_dt,
                })
        return articles
    except Exception as e:
        print(f"RSS 錯誤 {url[:60]}：{e}")
        return []

def fetch_all_news(category: str, session_info: dict) -> list:
    feeds = RSS_FEEDS.get(category, [])
    all_articles = []
    seen_titles = set()

    for url in feeds:
        articles = fetch_rss(url, limit=15)
        for a in articles:
            title = a["title"]
            title_key = re.sub(r"\s+", "", title.lower())[:30]
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            all_articles.append(a)
        time.sleep(0.3)

    now = now_tw()
    start_h = session_info["start_h"]
    end_h = session_info["end_h"]

    if start_h < end_h:
        start_dt = now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
    else:
        yesterday = now - datetime.timedelta(days=1)
        start_dt = yesterday.replace(hour=start_h, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=end_h, minute=0, second=0, microsecond=0)

    filtered = []
    no_time = []
    for a in all_articles:
        if a["pub_dt"] is None:
            no_time.append(a)
        elif start_dt <= a["pub_dt"] <= end_dt:
            filtered.append(a)

    result = filtered + no_time
    result.sort(key=lambda x: x["pub_dt"] or datetime.datetime.min.replace(tzinfo=TW_TZ), reverse=True)
    return result[:15]

# ─────────────────────────────────────────────────────────────
# Claude 分析
# ─────────────────────────────────────────────────────────────
def build_news_text(articles: list, limit: int = 12) -> str:
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        title = a["title"]
        summary = a.get("summary", "")
        pub = a["pub_dt"].strftime("%m/%d %H:%M") if a["pub_dt"] else ""
        line = f"{i}. [{pub}] {title}"
        if summary:
            line += f"\n   摘要：{summary[:100]}"
        lines.append(line)
    return "\n".join(lines) if lines else "（本時段暫無新聞）"

def analyze_tw_news(articles: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(articles)
    twii = market_data.get("twii", {})
    weekend_note = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    prompt = f"""你是一位資深台股分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{weekend_note}

【台灣加權指數】{fmt_quote(twii) if twii else 'N/A'}

【本時段台股重大新聞】
{news_text}

請以繁體中文撰寫台股分析日報，格式要求如下：

1. **市場概況**（2-3句，說明大盤走勢與成交量）

2. **重點個股利多/利空分析**
請用 Markdown 表格呈現，欄位：| 個股名稱（代號） | 方向 | 關鍵事件 | 影響評估 |
（至少列出 3-5 檔個股，個股名稱與代號請用粗體 **XX（XXXX）**）

3. **類股動態**（用表格呈現：| 類股 | 趨勢 | 主要驅動因素 |）

4. **操作建議**（2-3個重點，每點以 • 開頭）

5. **風險提示**（1-2句）

請確保分析具體、專業，並直接引用新聞中的公司名稱與事件。"""
    return claude_call(prompt, max_tokens=1800)

def analyze_us_news(articles: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(articles)
    dji = market_data.get("dji", {})
    ixic = market_data.get("ixic", {})
    gspc = market_data.get("gspc", {})
    weekend_note = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    prompt = f"""你是一位資深美股分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{weekend_note}

【美股大盤】
- 道瓊：{fmt_quote(dji) if dji else 'N/A'}
- 納斯達克：{fmt_quote(ixic) if ixic else 'N/A'}
- S&P 500：{fmt_quote(gspc) if gspc else 'N/A'}

【本時段美股重大新聞】
{news_text}

請以繁體中文撰寫美股分析日報，格式要求如下：

1. **市場概況**（2-3句，說明三大指數走勢與市場情緒）

2. **重點個股利多/利空分析**
請用 Markdown 表格呈現，欄位：| 個股名稱（代號） | 方向 | 關鍵事件 | 影響評估 |
（至少列出 3-5 檔個股，個股名稱與代號請用粗體 **XX（XXXX）**）

3. **產業板塊輪動**（用表格呈現：| 板塊 | 表現 | 主要驅動因素 |）

4. **Fed 政策與總經觀察**（2-3句）

5. **操作建議**（2-3個重點，每點以 • 開頭）

請確保分析具體、專業，並直接引用新聞中的公司名稱與事件。"""
    return claude_call(prompt, max_tokens=1800)

def analyze_global_news(articles: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(articles)
    vix = market_data.get("vix", {})
    gold = market_data.get("gold", {})
    oil = market_data.get("oil", {})
    dxy = market_data.get("dxy", {})
    us10y = market_data.get("us10y", {})
    crypto = market_data.get("crypto", {})
    btc = crypto.get("BTC", {})
    eth = crypto.get("ETH", {})
    weekend_note = "\n⚠️ 今日為週末，部分指標數據為上次交易日數值，已標註「未更新」。" if is_weekend() else ""

    prompt = f"""你是一位資深國際財經分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{weekend_note}

【全球關鍵指標】
- VIX 恐慌指數：{fmt_quote(vix) if vix else 'N/A'}
- 美債 10Y 殖利率：{fmt_quote(us10y) if us10y else 'N/A'}
- 美元指數 DXY：{fmt_quote(dxy) if dxy else 'N/A'}
- 黃金：{fmt_quote(gold) if gold else 'N/A'}
- 原油（WTI）：{fmt_quote(oil) if oil else 'N/A'}
- BTC：{btc.get('emoji','') + ' ' + btc.get('price','N/A') + ' (' + btc.get('pct','N/A') + ')' if btc else 'N/A'}
- ETH：{eth.get('emoji','') + ' ' + eth.get('price','N/A') + ' (' + eth.get('pct','N/A') + ')' if eth else 'N/A'}

【本時段國際重大新聞】
{news_text}

請以繁體中文撰寫國際財經分析日報，格式要求如下：

1. **全球市場情緒**（2-3句，綜合 VIX、美債、美元走勢）

2. **重大地緣政治與總經事件**
請用 Markdown 表格呈現，欄位：| 事件 | 影響資產 | 利多/利空 | 影響程度 |
（至少列出 3-5 個事件）

3. **大宗商品與加密貨幣**（用表格呈現：| 品項 | 價格 | 漲跌 | 關鍵驅動 |）

4. **對台股/亞股的影響**（2-3個重點，每點以 • 開頭）

5. **本週重要財經數據預告**（若有）

請確保分析具體、專業，並直接引用新聞中的事件。"""
    return claude_call(prompt, max_tokens=1800)

# ─────────────────────────────────────────────────────────────
# 自選股分析
# ─────────────────────────────────────────────────────────────
def analyze_single_stock(symbol: str, name: str, quote: dict, news_context: str = "") -> str:
    """Claude 分析單一股票"""
    stale_note = "⚠️ 注意：今日為週末，以下為上次交易日收盤數據。\n" if quote.get("stale") else ""
    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析與預測。

{stale_note}
【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote.get('price', 'N/A')}
- 漲跌幅：{quote.get('pct', 'N/A')}
- 漲跌點：{quote.get('change', 'N/A')}

【相關新聞背景】
{news_context if news_context else '（暫無相關新聞）'}

請以繁體中文撰寫分析報告，格式如下：

1. **近期走勢分析**（2-3句，說明目前價格位置與趨勢）

2. **技術面觀察**（提及均線、支撐壓力位、RSI/MACD 等技術指標的可能狀態）

3. **基本面亮點**（2-3個重點，結合近期新聞或產業動態）

4. **短期預測**（未來 1-2 週的可能走向，給出目標價區間）

5. **操作建議**
   - 多方策略：...
   - 空方策略：...
   - 風險提示：...

請保持專業、客觀，並明確標示這是 AI 分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1200)

def fetch_stock_news(symbol: str, name: str) -> str:
    """抓取特定股票的相關新聞"""
    query = f"{name} {symbol} stock"
    if ".TW" in symbol:
        query = f"{name} 台股 股票"
    try:
        url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        titles = [e.title for e in feed.entries[:5] if hasattr(e, "title")]
        return "\n".join(titles) if titles else ""
    except Exception:
        return ""

def run_watchlist_report():
    """執行自選股日報（每日結算後）"""
    print("\n📈 執行自選股分析...")
    session_info = get_session_info()
    ts = now_str()
    weekend_banner = "（週末版 — 數據為上次交易日）" if is_weekend() else ""

    # 發送標題訊息
    send_embed(DISCORD_WATCHLIST, {
        "title": f"📊 自選股日報 {session_info['emoji']} {session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}\n以下為自選股最新分析，由 Claude AI 生成，不構成投資建議。",
        "color": 0xF39C12,
        "footer": {"text": "資料來源：Yahoo Finance + Claude AI"},
    })
    time.sleep(1.2)

    for stock in WATCHLIST:
        symbol = stock["symbol"]
        name = stock["name"]
        print(f"  分析 {name}（{symbol}）...")

        # 抓取報價
        quote = fetch_yahoo(symbol, name)
        print(f"    價格：{fmt_quote(quote)}")

        # 抓取相關新聞
        news_ctx = fetch_stock_news(symbol, name)
        time.sleep(0.5)

        # Claude 分析
        analysis = analyze_single_stock(symbol, name, quote, news_ctx)
        time.sleep(1)

        # 發送到 Discord
        stale_tag = " ⚠️未更新" if quote.get("stale") else ""
        send_discord_message(
            DISCORD_WATCHLIST,
            f"## {quote.get('emoji','📊')} **{name}（{symbol}）** — {quote.get('price','N/A')} ({quote.get('pct','N/A')}){stale_tag}\n\n{analysis}"
        )
        time.sleep(1.5)

    print("  ✅ 自選股分析完成")

# ─────────────────────────────────────────────────────────────
# Discord 傳送
# ─────────────────────────────────────────────────────────────
MAX_EMBED_FIELD = 1024
MAX_CONTENT = 2000

def truncate(text: str, limit: int = MAX_EMBED_FIELD) -> str:
    if not text or not text.strip():
        return "暫無資料"
    text = text.strip()
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text

def send_discord_message(webhook_url: str, content: str) -> bool:
    if not content or not content.strip():
        return False
    chunks = []
    lines = content.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > MAX_CONTENT:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    success = True
    for chunk in chunks:
        try:
            r = requests.post(webhook_url, json={"content": chunk}, timeout=15)
            if r.status_code not in [200, 204]:
                print(f"❌ Discord 傳送失敗：{r.status_code} {r.text[:200]}")
                success = False
            else:
                print(f"✅ Discord 傳送成功（{len(chunk)} 字）")
        except Exception as e:
            print(f"❌ Discord 傳送錯誤：{e}")
            success = False
        time.sleep(1.0)
    return success

def send_embed(webhook_url: str, embed: dict) -> bool:
    for field in embed.get("fields", []):
        v = field.get("value", "")
        field["value"] = truncate(v, MAX_EMBED_FIELD)
    try:
        r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
        if r.status_code in [200, 204]:
            print(f"✅ Embed 發送成功：{embed.get('title', '')[:40]}")
            return True
        else:
            print(f"❌ Embed 發送失敗：{r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        print(f"❌ Embed 發送錯誤：{e}")
        return False

def build_news_links(articles: list, limit: int = 8) -> str:
    if not articles:
        return "暫無新聞"
    lines = []
    for i, a in enumerate(articles[:limit], 1):
        title = a["title"][:50]
        url = a.get("url", "#")
        pub = a["pub_dt"].strftime("%H:%M") if a["pub_dt"] else ""
        time_tag = f"`{pub}` " if pub else ""
        lines.append(f"{i}. {time_tag}[{title}]({url})")
    return "\n".join(lines)

def build_market_table(market_data: dict) -> str:
    rows = []
    mapping = [
        ("twii",  "🇹🇼 台灣加權"),
        ("dji",   "🇺🇸 道瓊"),
        ("ixic",  "📊 納斯達克"),
        ("gspc",  "📈 S&P 500"),
        ("vix",   "😱 VIX"),
        ("us10y", "🏦 美債10Y"),
        ("dxy",   "💵 美元指數"),
        ("gold",  "🥇 黃金"),
        ("oil",   "🛢️ 原油(WTI)"),
    ]
    for key, label in mapping:
        q = market_data.get(key, {})
        if q and q.get("price") != "N/A":
            stale = " ⚠️未更新" if q.get("stale") else ""
            rows.append(f"{label}: {q['emoji']} **{q['price']}** ({q['pct']}){stale}")
    return "\n".join(rows) if rows else "暫無數據"

# ─────────────────────────────────────────────────────────────
# 主執行函式
# ─────────────────────────────────────────────────────────────
def run_report():
    """執行一次完整的日報生成與發送"""
    print("=" * 65)
    session_info = get_session_info()
    weekend_note = "（週末版）" if is_weekend() else ""
    print(f"🚀 股市日報啟動 — {now_str()} {weekend_note}")
    print(f"📅 本次時段：{session_info['emoji']} {session_info['label']} ({session_info['period']})")
    print("=" * 65)

    # ── 1. 抓取大盤數據 ──────────────────────────────────────
    print("\n📊 抓取大盤數據...")
    market_data = {}
    symbols = {
        "twii":  ("^TWII",    "台灣加權"),
        "dji":   ("^DJI",     "道瓊"),
        "ixic":  ("^IXIC",    "納斯達克"),
        "gspc":  ("^GSPC",    "S&P 500"),
        "vix":   ("^VIX",     "VIX"),
        "us10y": ("^TNX",     "美債10Y"),
        "dxy":   ("DX-Y.NYB", "美元指數"),
        "gold":  ("GC=F",     "黃金"),
        "oil":   ("CL=F",     "原油"),
    }
    for key, (sym, name) in symbols.items():
        market_data[key] = fetch_yahoo(sym, name)
        stale_tag = " ⚠️未更新" if market_data[key].get("stale") else ""
        print(f"  {name}: {fmt_quote(market_data[key])}{stale_tag}")
        time.sleep(0.2)

    # ── 2. 抓取加密貨幣 ──────────────────────────────────────
    print("\n🪙 抓取加密貨幣...")
    crypto = fetch_crypto()
    market_data["crypto"] = crypto
    for sym, data in crypto.items():
        print(f"  {sym}: {data['emoji']} {data['price']} ({data['pct']})")

    # ── 3. 抓取新聞 ──────────────────────────────────────────
    print("\n📰 抓取新聞（RSS）...")
    tw_news     = fetch_all_news("tw", session_info)
    us_news     = fetch_all_news("us", session_info)
    global_news = fetch_all_news("global", session_info)
    print(f"  台股：{len(tw_news)} 篇 | 美股：{len(us_news)} 篇 | 國際：{len(global_news)} 篇")

    # ── 4. Claude 分析 ───────────────────────────────────────
    print("\n🤖 Claude AI 分析中...")
    print("  分析台股...")
    tw_analysis = analyze_tw_news(tw_news, session_info, market_data)
    time.sleep(1)
    print("  分析美股...")
    us_analysis = analyze_us_news(us_news, session_info, market_data)
    time.sleep(1)
    print("  分析國際...")
    global_analysis = analyze_global_news(global_news, session_info, market_data)

    # ── 5. 組合訊息 ──────────────────────────────────────────
    weekend_banner = "（週末版）" if is_weekend() else ""
    session_tag = f"{session_info['emoji']} {session_info['label']} {weekend_banner}| {session_info['period']}"
    ts = now_str()

    market_table = build_market_table(market_data)

    crypto_text = "\n".join([
        f"{d['emoji']} **{sym}**：{d['price']} ({d['pct']})"
        for sym, d in crypto.items()
    ]) if crypto else "暫無數據"

    tw_links     = build_news_links(tw_news)
    us_links     = build_news_links(us_news)
    global_links = build_news_links(global_news)

    # ── 6. 發送到 Discord ────────────────────────────────────
    print("\n📤 發送到 Discord...")

    # ── 台股頻道 ──────────────────────────────────────────────
    send_embed(DISCORD_TW, {
        "title": f"🇹🇼 台股{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color": 0x2ECC71,
        "fields": [
            {"name": "📊 大盤指數", "value": truncate(market_table), "inline": False},
        ],
        "footer": {"text": "資料來源：Yahoo Finance" + ("｜⚠️ 週末數據為上次交易日" if is_weekend() else "")},
    })
    time.sleep(1.2)

    header = f"## 🤖 Claude AI 台股分析 — {session_tag}\n\n"
    send_discord_message(DISCORD_TW, header + tw_analysis)
    time.sleep(1.2)

    send_embed(DISCORD_TW, {
        "title": f"📰 台股新聞連結 | {ts}",
        "color": 0x27AE60,
        "fields": [
            {"name": "🔗 本時段重要新聞", "value": truncate(tw_links), "inline": False},
        ],
        "footer": {"text": "資料來源：Google News / 鉅亨網 / MoneyDJ"},
    })
    time.sleep(1.5)

    # ── 美股頻道 ──────────────────────────────────────────────
    us_market_text = "\n".join([
        f"{market_data['dji']['emoji']} **道瓊**：{market_data['dji']['price']} ({market_data['dji']['pct']})" + (" ⚠️未更新" if market_data['dji'].get('stale') else ""),
        f"{market_data['ixic']['emoji']} **納斯達克**：{market_data['ixic']['price']} ({market_data['ixic']['pct']})" + (" ⚠️未更新" if market_data['ixic'].get('stale') else ""),
        f"{market_data['gspc']['emoji']} **S&P 500**：{market_data['gspc']['price']} ({market_data['gspc']['pct']})" + (" ⚠️未更新" if market_data['gspc'].get('stale') else ""),
    ])

    send_embed(DISCORD_US, {
        "title": f"🇺🇸 美股{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color": 0x3498DB,
        "fields": [
            {"name": "📊 三大指數", "value": truncate(us_market_text), "inline": False},
        ],
        "footer": {"text": "資料來源：Yahoo Finance" + ("｜⚠️ 週末數據為上次交易日" if is_weekend() else "")},
    })
    time.sleep(1.2)

    header = f"## 🤖 Claude AI 美股分析 — {session_tag}\n\n"
    send_discord_message(DISCORD_US, header + us_analysis)
    time.sleep(1.2)

    send_embed(DISCORD_US, {
        "title": f"📰 美股新聞連結 | {ts}",
        "color": 0x2980B9,
        "fields": [
            {"name": "🔗 本時段重要新聞", "value": truncate(us_links), "inline": False},
        ],
        "footer": {"text": "資料來源：Yahoo Finance / MarketWatch / Google News"},
    })
    time.sleep(1.5)

    # ── 國際頻道 ──────────────────────────────────────────────
    global_market_text = "\n".join([
        f"{market_data['vix']['emoji']} **VIX**：{market_data['vix']['price']} ({market_data['vix']['pct']})" + (" ⚠️未更新" if market_data['vix'].get('stale') else ""),
        f"{market_data['us10y']['emoji']} **美債10Y**：{market_data['us10y']['price']} ({market_data['us10y']['pct']})" + (" ⚠️未更新" if market_data['us10y'].get('stale') else ""),
        f"{market_data['dxy']['emoji']} **美元指數**：{market_data['dxy']['price']} ({market_data['dxy']['pct']})" + (" ⚠️未更新" if market_data['dxy'].get('stale') else ""),
        f"{market_data['gold']['emoji']} **黃金**：{market_data['gold']['price']} ({market_data['gold']['pct']})" + (" ⚠️未更新" if market_data['gold'].get('stale') else ""),
        f"{market_data['oil']['emoji']} **原油(WTI)**：{market_data['oil']['price']} ({market_data['oil']['pct']})" + (" ⚠️未更新" if market_data['oil'].get('stale') else ""),
    ])

    send_embed(DISCORD_GLOBAL, {
        "title": f"🌍 國際市場{session_info['label']} {weekend_banner}| {ts}",
        "description": f"**時段：** {session_info['period']}",
        "color": 0x9B59B6,
        "fields": [
            {"name": "📉 總體指標", "value": truncate(global_market_text), "inline": True},
            {"name": "🪙 加密貨幣", "value": truncate(crypto_text), "inline": True},
        ],
        "footer": {"text": "資料來源：Yahoo Finance / CoinGecko" + ("｜⚠️ 週末數據為上次交易日" if is_weekend() else "")},
    })
    time.sleep(1.2)

    header = f"## 🤖 Claude AI 國際分析 — {session_tag}\n\n"
    send_discord_message(DISCORD_GLOBAL, header + global_analysis)
    time.sleep(1.2)

    send_embed(DISCORD_GLOBAL, {
        "title": f"📰 國際新聞連結 | {ts}",
        "color": 0x8E44AD,
        "fields": [
            {"name": "🔗 本時段重要新聞", "value": truncate(global_links), "inline": False},
        ],
        "footer": {"text": "資料來源：Reuters / BBC / Google News"},
    })

    # ── 7. 自選股分析（每日 22:00 盤後執行）──────────────────
    if session_info["label"] == "盤後晚報":
        time.sleep(2)
        run_watchlist_report()

    print("\n" + "=" * 65)
    print(f"✅ 日報完成！— {now_str()}")
    print("=" * 65)

# ─────────────────────────────────────────────────────────────
# 排程設定（每天，含週六日）
# ─────────────────────────────────────────────────────────────
def run_schedule():
    """啟動排程模式（每日 08:00 / 14:00 / 22:00 台灣時間，含週六日）"""
    print("=" * 65)
    print("⏰ 排程模式啟動")
    print("   每日 08:00 / 14:00 / 22:00（台灣時間）自動執行")
    print("   週六日照常執行，指標數據標註「未更新」")
    print("   按 Ctrl+C 停止")
    print("=" * 65)

    schedule.every().day.at("08:00").do(run_report)
    schedule.every().day.at("14:00").do(run_report)
    schedule.every().day.at("22:00").do(run_report)

    next_run = schedule.next_run()
    print(f"⏭️  下次執行：{next_run}")

    while True:
        schedule.run_pending()
        time.sleep(30)

# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("⚠️  警告：未設定 ANTHROPIC_API_KEY 環境變數！")
        print("   請執行：$env:ANTHROPIC_API_KEY='your-api-key-here'")
        print("   AI 分析功能將無法使用。\n")

    if "--schedule" in sys.argv:
        run_schedule()
    else:
        print("💡 提示：加上 --schedule 參數可啟動自動排程模式")
        print("   例如：python stock_daily.py --schedule\n")
        run_report()
