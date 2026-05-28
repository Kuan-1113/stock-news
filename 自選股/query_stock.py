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

_BALANCE_KEYWORDS = ("credit", "balance", "billing", "payment", "insufficient")


def _notify_balance_low_query():
    """查股觸發餘額不足時，立即發 Discord 通知（每次查股各自觸發，但訊息本身已夠清楚）。"""
    msg = (
        "🚨 **Claude API 餘額不足 — 查股分析已中斷**\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"偵測時間：{datetime.datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')}\n"
        "請前往 https://console.anthropic.com/settings/billing 儲值後重新查詢。"
    )
    if DISCORD_WATCHLIST:
        try:
            requests.post(DISCORD_WATCHLIST, json={"content": msg}, timeout=10)
        except Exception:
            pass


def claude_call(prompt: str, max_tokens: int = 1200) -> str:
    """
    呼叫 Claude API 並分析。
    餘額不足（HTTP 400/402）→ 回傳明確提示 + 觸發 Discord 通知。
    """
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，請聯絡管理員。"
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

        err_body = r.text.lower()

        # 餘額不足
        if r.status_code in (400, 402) and any(kw in err_body for kw in _BALANCE_KEYWORDS):
            _notify_balance_low_query()
            return "🚨 Claude API 餘額不足，分析功能暫停。請至 console.anthropic.com 儲值。"

        # 速率限制
        if r.status_code == 429:
            return "⏳ 查詢過於頻繁（rate limit），請稍後再試。"

        return f"⚠️ AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        return f"⚠️ AI 分析暫時無法使用（連線異常：{str(e)[:80]}）"

# ── 基本面（Yahoo Finance fundamentals）─────────────────────

def fetch_fundamentals(symbol: str) -> dict:
    """從 Yahoo Finance summary API 取得 PE、EPS、ROE、PBR、市值等基本面數據"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
            "?modules=defaultKeyStatistics,financialData,summaryDetail",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return {}
        result = r.json().get("quoteSummary", {}).get("result", [{}])[0]
        ks  = result.get("defaultKeyStatistics", {})
        fd  = result.get("financialData", {})
        sd  = result.get("summaryDetail", {})

        def _v(obj, key):
            val = obj.get(key, {})
            if isinstance(val, dict):
                return val.get("fmt") or val.get("raw")
            return val

        return {
            "pe":           _v(sd, "trailingPE"),
            "forward_pe":   _v(sd, "forwardPE"),
            "eps":          _v(ks, "trailingEps"),
            "pbr":          _v(ks, "priceToBook"),
            "roe":          _v(fd, "returnOnEquity"),
            "profit_margin":_v(fd, "profitMargins"),
            "revenue_growth":_v(fd, "revenueGrowth"),
            "market_cap":   _v(sd, "marketCap"),
            "52w_high":     _v(sd, "fiftyTwoWeekHigh"),
            "52w_low":      _v(sd, "fiftyTwoWeekLow"),
        }
    except Exception as e:
        print(f"基本面錯誤：{e}")
        return {}

def format_fundamentals(f: dict) -> str:
    """格式化基本面數據為 prompt 文字"""
    if not f:
        return "（基本面資料暫無）"
    lines = []
    if f.get("pe"):          lines.append(f"• 本益比（TTM）：{f['pe']}")
    if f.get("forward_pe"):  lines.append(f"• 預估本益比：{f['forward_pe']}")
    if f.get("eps"):         lines.append(f"• EPS（TTM）：{f['eps']}")
    if f.get("pbr"):         lines.append(f"• 股價淨值比：{f['pbr']}")
    if f.get("roe"):         lines.append(f"• 股東權益報酬率（ROE）：{float(f['roe']):.1%}" if isinstance(f['roe'], (int, float)) else f"• ROE：{f['roe']}")
    if f.get("profit_margin"): lines.append(f"• 淨利率：{float(f['profit_margin']):.1%}" if isinstance(f['profit_margin'], (int, float)) else f"• 淨利率：{f['profit_margin']}")
    if f.get("revenue_growth"): lines.append(f"• 營收成長：{float(f['revenue_growth']):.1%}" if isinstance(f['revenue_growth'], (int, float)) else f"• 營收成長：{f['revenue_growth']}")
    if f.get("52w_high"):    lines.append(f"• 52週高點：{f['52w_high']}")
    if f.get("52w_low"):     lines.append(f"• 52週低點：{f['52w_low']}")
    return "\n".join(lines) if lines else "（基本面資料暫無）"


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

def analyze(symbol: str, name: str, quote: dict, news: str, ind: dict = None, fund: dict = None) -> str:
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    indicators_text  = format_indicators_for_prompt(ind) if ind else "（未取得技術指標）"
    fundamentals_text = format_fundamentals(fund) if fund else "（未取得基本面）"

    prompt = f"""你是一位資深股票分析師，請針對以下股票進行完整深度分析。每項指標**必須帶實際數值**，禁空話，全程禁用 Markdown 表格（只用 bullet）。

【股票資訊】
- 名稱：{name}（{symbol}）
- 最新價格：{quote['price']} {quote.get('currency', '')}
- 漲跌：{quote['change']} ({quote['pct']})
- {price_history}

【技術指標（實際計算值）】
{indicators_text}

【基本面數據】
{fundamentals_text}

【近期相關新聞（消息面）】
{news}

請以繁體中文撰寫完整分析報告（1000字以內），以下各節**必須全部輸出**：

📊 **近期走勢**（2-3句，說明目前價格位置與方向）

🔍 **技術面分析**（每項帶實際數值）
• 均線 MA5/MA20/MA60 → 多空排列
• MACD 線/訊號線/柱 → 動能方向
• RSI 數值 → 超買/超賣/中性
• KD K/D值 → 黃金/死亡交叉
• 成交量 vs 均量 → 量價配合
• 乖離率 → 偏離程度
• 融資券動向（若有台股資料）
• 三大法人動向（若有台股資料）

💰 **基本面分析**
• 估值：PE / PBR 相較產業是偏高/合理/偏低
• 獲利：EPS / ROE / 淨利率說明獲利品質
• 成長：營收成長率，是否持續擴張
• 52週位置：目前股價在年度高低點之間的位置

📰 **消息面分析**（結合上方新聞，每則新聞說明其對股價影響方向，2-4點）

🎯 **短期預測（1-2週）**
• 多方情境：目標價（依據技術位或基本面）
• 空方情境：跌破關鍵支撐位

💡 **操作建議**（bullet，不用表格）
• 積極進場條件：...
• 目標價：...
• 停損參考：...

⚠️ 本報告由 AI 生成，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1600)

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

    # 基本面數據
    print("💰 抓取基本面數據...")
    fund = fetch_fundamentals(SYMBOL)

    # AI 分析
    print("🤖 Claude 分析中...")
    analysis = analyze(SYMBOL, display_name, quote, news, ind, fund)

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
