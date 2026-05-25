"""
discord_bot.py
Discord Bot - /查股 指令
支援查詢任意股票代碼，由 Claude AI 生成分析報告

部署方式：Render（免費方案）
環境變數：
  DISCORD_BOT_TOKEN  - Discord Bot Token
  ANTHROPIC_API_KEY  - Claude API Key
"""

import os
import sys
import re
import time
import datetime
import asyncio
import requests
import feedparser
import anthropic
import discord
from discord import app_commands

# ─────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

# ─────────────────────────────────────────────────────────────
# Claude 客戶端
# ─────────────────────────────────────────────────────────────
def get_claude_client():
    if not ANTHROPIC_API_KEY:
        return None
    try:
        return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    except Exception:
        return None

claude_client = get_claude_client()

def claude_call(prompt: str, max_tokens: int = 1200) -> str:
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
        return f"AI 分析暫時無法使用（{str(e)[:100]}）"

# ─────────────────────────────────────────────────────────────
# 股票數據
# ─────────────────────────────────────────────────────────────
def fetch_yahoo(symbol: str) -> dict:
    """抓取 Yahoo Finance 報價"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10d"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0].get("volume", [])
        closes = [c for c in closes if c is not None]
        meta = result.get("meta", {})

        if len(closes) >= 2:
            prev, curr = closes[-2], closes[-1]
            chg = curr - prev
            pct = chg / prev * 100
            emoji = "🔴" if pct < 0 else "🟢"

            # 計算簡單均線
            ma5  = sum(closes[-5:])  / min(5,  len(closes)) if len(closes) >= 1 else None
            ma10 = sum(closes[-10:]) / min(10, len(closes)) if len(closes) >= 1 else None

            # 最新成交量
            vols = [v for v in volumes if v is not None]
            vol_str = f"{vols[-1]:,.0f}" if vols else "N/A"

            return {
                "symbol": symbol,
                "name": meta.get("shortName", symbol),
                "price": curr,
                "price_str": f"{curr:,.2f}",
                "change": chg,
                "change_str": f"{chg:+.2f}",
                "pct": pct,
                "pct_str": f"{pct:+.2f}%",
                "emoji": emoji,
                "currency": meta.get("currency", ""),
                "ma5": f"{ma5:,.2f}" if ma5 else "N/A",
                "ma10": f"{ma10:,.2f}" if ma10 else "N/A",
                "volume": vol_str,
                "closes": closes,
            }
        return None
    except Exception as e:
        print(f"Yahoo 錯誤 {symbol}：{e}")
        return None

def fetch_stock_news_for_bot(symbol: str, name: str) -> str:
    """抓取股票相關新聞"""
    queries = []
    if ".TW" in symbol:
        queries = [
            f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' 台股')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
            f"https://news.google.com/rss/search?q={requests.utils.quote(symbol)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        ]
    else:
        queries = [
            f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' stock')}&hl=en-US&gl=US&ceid=US:en",
            f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' earnings')}&hl=en-US&gl=US&ceid=US:en",
        ]

    titles = []
    for url in queries[:2]:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:4]:
                if hasattr(e, "title") and e.title not in titles:
                    titles.append(e.title)
            time.sleep(0.3)
        except Exception:
            pass

    return "\n".join(titles[:6]) if titles else "（暫無相關新聞）"

def analyze_stock_for_bot(symbol: str, quote: dict, news_ctx: str) -> str:
    """Claude 分析股票（Bot 版本）"""
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析與預測。

【股票資訊】
- 代碼：{symbol}
- 名稱：{quote.get('name', symbol)}
- 最新價格：{quote['price_str']} {quote.get('currency', '')}
- 漲跌：{quote['change_str']} ({quote['pct_str']})
- 5日均線：{quote.get('ma5', 'N/A')}
- 10日均線：{quote.get('ma10', 'N/A')}
- 成交量：{quote.get('volume', 'N/A')}
- {price_history}

【近期相關新聞】
{news_ctx}

請以繁體中文撰寫分析報告（控制在 800 字以內），格式如下：

📊 **近期走勢**
（2-3句說明目前價格位置與趨勢方向）

🔍 **技術面分析**
（均線多空排列、支撐壓力位、RSI/MACD 可能狀態）

📰 **基本面亮點**
（結合近期新聞，2-3個重點）

🎯 **短期預測（1-2週）**
- 目標價區間：...
- 可能走向：...

💡 **操作建議**
- 多方：...
- 空方：...
- 停損參考：...

⚠️ 本報告由 AI 生成，不構成投資建議，請自行判斷風險。"""

    return claude_call(prompt, max_tokens=1200)

# ─────────────────────────────────────────────────────────────
# Discord Bot
# ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot 已上線：{client.user}")
    print(f"   已同步 Slash Commands")

@tree.command(name="查股", description="查詢股票分析（台股用 2330.TW，美股用 NVDA）")
@app_commands.describe(代碼="股票代碼，例如：2330.TW、NVDA、AAPL、TSLA")
async def query_stock(interaction: discord.Interaction, 代碼: str):
    symbol = 代碼.strip().upper()

    # 先回應「處理中」避免 Discord 3秒超時
    await interaction.response.defer(thinking=True)

    try:
        # 抓取報價
        quote = await asyncio.get_event_loop().run_in_executor(None, fetch_yahoo, symbol)

        if not quote:
            await interaction.followup.send(
                f"❌ 找不到股票代碼 `{symbol}`\n"
                f"請確認格式：\n"
                f"• 台股：`2330.TW`、`2454.TW`\n"
                f"• 美股：`NVDA`、`AAPL`、`TSLA`\n"
                f"• 指數：`^TWII`、`^DJI`、`^IXIC`"
            )
            return

        # 抓取新聞
        news_ctx = await asyncio.get_event_loop().run_in_executor(
            None, fetch_stock_news_for_bot, symbol, quote.get("name", symbol)
        )

        # Claude 分析
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, analyze_stock_for_bot, symbol, quote, news_ctx
        )

        # 組合訊息
        now_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        header = (
            f"## {quote['emoji']} **{quote.get('name', symbol)}（{symbol}）**\n"
            f"💰 **{quote['price_str']}** {quote.get('currency', '')}　"
            f"{quote['change_str']} ({quote['pct_str']})\n"
            f"📅 查詢時間：{now_str}\n\n"
        )

        full_msg = header + analysis

        # Discord 訊息上限 2000 字，分段發送
        if len(full_msg) <= 2000:
            await interaction.followup.send(full_msg)
        else:
            # 分段
            chunks = []
            lines = full_msg.split("\n")
            current = ""
            for line in lines:
                if len(current) + len(line) + 1 > 1900:
                    if current:
                        chunks.append(current)
                    current = line
                else:
                    current = current + "\n" + line if current else line
            if current:
                chunks.append(current)

            for chunk in chunks:
                await interaction.followup.send(chunk)
                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"❌ /查股 錯誤：{e}")
        await interaction.followup.send(f"❌ 查詢時發生錯誤：{str(e)[:200]}")

@tree.command(name="自選股", description="立即查看所有自選股的最新報價")
async def watchlist_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    WATCHLIST = [
        {"symbol": "2330.TW", "name": "台積電"},
        {"symbol": "2454.TW", "name": "聯發科"},
        {"symbol": "NVDA",    "name": "輝達"},
        {"symbol": "AAPL",    "name": "蘋果"},
        {"symbol": "TSLA",    "name": "特斯拉"},
        {"symbol": "^TWII",   "name": "加權指數"},
    ]

    lines = []
    for stock in WATCHLIST:
        q = await asyncio.get_event_loop().run_in_executor(None, fetch_yahoo, stock["symbol"])
        if q:
            lines.append(
                f"{q['emoji']} **{stock['name']}（{stock['symbol']}）**：{q['price_str']} ({q['pct_str']})"
            )
        else:
            lines.append(f"⚪ **{stock['name']}（{stock['symbol']}）**：無法取得")
        await asyncio.sleep(0.3)

    now_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    msg = f"## 📊 自選股即時報價 | {now_str}\n\n" + "\n".join(lines)
    await interaction.followup.send(msg)

# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("❌ 未設定 DISCORD_BOT_TOKEN 環境變數！")
        print("   請到 GitHub Secrets 新增 DISCORD_BOT_TOKEN")
        sys.exit(1)

    if not ANTHROPIC_API_KEY:
        print("⚠️  未設定 ANTHROPIC_API_KEY，AI 分析功能將無法使用")

    print("🤖 Discord Bot 啟動中...")
    client.run(DISCORD_BOT_TOKEN)
