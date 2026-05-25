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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from technical_indicators import get_full_indicators, format_indicators_for_prompt, format_indicators_for_discord

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
    """多源抓取個股新聞（Google News + 鉅亨網 + Yahoo Finance）"""
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
        _add(f"https://feeds.content.dowjones.io/public/rss/mw_topstories")

    return "\n".join(titles[:8]) if titles else "（暫無相關新聞）"

def analyze_stock_for_bot(symbol: str, quote: dict, news_ctx: str, ind: dict = None) -> str:
    """Claude 分析股票（Bot 版本）"""
    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    indicators_text = format_indicators_for_prompt(ind) if ind else "（未取得技術指標）"

    prompt = f"""你是一位資深股票分析師。請針對以下股票進行深度分析與預測。

【股票資訊】
- 代碼：{symbol}
- 名稱：{quote.get('name', symbol)}
- 最新價格：{quote['price_str']} {quote.get('currency', '')}
- 漲跌：{quote['change_str']} ({quote['pct_str']})
- {price_history}

【技術指標（實際計算值）】
{indicators_text}

【近期相關新聞（多方查證）】
{news_ctx}

請以繁體中文撰寫分析報告（900 字以內），依據上方**實際技術指標數值**進行判斷：

📊 **近期走勢**（2-3句，說明目前價格與均線關係）

🔍 **技術面分析**
- 均線多空排列
- MACD 動能（多空判斷）
- RSI 位置
- KDJ 訊號
- 乖離率（偏離程度）
- 成交量（量能配合度）
- 融資券動向（若有）
- 三大法人動向（若有）

📰 **基本面亮點**（結合近期新聞，2-3點）

🎯 **短期預測（1-2週）**
- 目標價區間：...
- 可能走向：...

💡 **操作建議**
- 多方：...
- 空方或觀望：...
- 停損參考：...

⚠️ 本報告由 AI 生成，不構成投資建議，請自行判斷風險。"""

    return claude_call(prompt, max_tokens=1400)

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

        # 技術指標
        ind = await asyncio.get_event_loop().run_in_executor(
            None, get_full_indicators, symbol
        )

        # Claude 分析
        analysis = await asyncio.get_event_loop().run_in_executor(
            None, analyze_stock_for_bot, symbol, quote, news_ctx, ind
        )

        # 組合訊息
        now_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
        header = (
            f"## {quote['emoji']} **{quote.get('name', symbol)}（{symbol}）**\n"
            f"💰 **{quote['price_str']}** {quote.get('currency', '')}　"
            f"{quote['change_str']} ({quote['pct_str']})\n"
            f"📅 查詢時間：{now_str}\n\n"
        )
        indicators_block = format_indicators_for_discord(ind) + "\n\n---\n\n"

        full_msg = header + indicators_block + analysis

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

def _load_watchlist() -> list:
    """讀取 watchlist.txt"""
    result = []
    try:
        watchlist_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.txt")
        with open(watchlist_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2:
                    result.append({"symbol": parts[0].upper(), "name": parts[1].strip()})
                elif len(parts) == 1:
                    result.append({"symbol": parts[0].upper(), "name": parts[0].upper()})
    except FileNotFoundError:
        result = [
            {"symbol": "2330.TW", "name": "台積電"},
            {"symbol": "2454.TW", "name": "聯發科"},
            {"symbol": "NVDA",    "name": "輝達"},
            {"symbol": "AAPL",    "name": "蘋果"},
            {"symbol": "TSLA",    "name": "特斯拉"},
            {"symbol": "^TWII",   "name": "加權指數"},
        ]
    return result


def _fetch_quote_and_indicators(stock: dict) -> str:
    """同步取得報價 + 指標，回傳 Discord 顯示字串"""
    symbol = stock["symbol"]
    name   = stock["name"]
    q = fetch_yahoo(symbol)
    if not q:
        return f"⚪ **{name}（{symbol}）**：無法取得報價"

    ind = get_full_indicators(symbol)
    m   = ind.get("macd", {})
    r   = ind.get("rsi",  {})
    k   = ind.get("kdj",  {})
    v   = ind.get("volume", {})

    macd_tag = ""
    if m.get("golden_cross"):
        macd_tag = " 🔔MACD黃金叉"
    elif m.get("dead_cross"):
        macd_tag = " 💀MACD死亡叉"

    rsi_val  = f"RSI `{r['rsi']}`" if r.get("rsi") else ""
    kdj_sig  = f"KDJ `{k['signal']}`" if k.get("signal") and k.get("k") else ""
    vol_tag  = v.get("vol_trend", "") if v.get("vol_ratio") else ""

    tags = " | ".join(t for t in [rsi_val, kdj_sig, vol_tag, macd_tag.strip()] if t)

    # 融資/三大法人摘要（台股）
    extra = ""
    inst = ind.get("institutional", {})
    if inst.get("total_net"):
        total = inst["total_net"].replace(",", "")
        try:
            icon = "🟢" if int(total) > 0 else "🔴"
            extra = f"\n　　法人合計：{icon} `{inst['total_net']}`"
        except Exception:
            pass

    return (
        f"{q['emoji']} **{name}（{symbol}）**　"
        f"💰 `{q['price_str']}` {q['change_str']} ({q['pct_str']}){macd_tag}\n"
        f"　　{tags}{extra}"
    )


@tree.command(name="自選股", description="查看所有自選股的最新報價與技術指標摘要")
async def watchlist_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    watchlist = _load_watchlist()
    now_str   = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

    await interaction.followup.send(f"## 📊 自選股即時報價與指標 | {now_str}\n⏳ 正在抓取資料，共 {len(watchlist)} 檔...")

    lines = []
    for stock in watchlist:
        line = await asyncio.get_event_loop().run_in_executor(None, _fetch_quote_and_indicators, stock)
        lines.append(line)
        await asyncio.sleep(0.5)

    msg = f"## 📊 自選股即時報價與指標 | {now_str}\n\n" + "\n\n".join(lines)

    # 分段發送（超過 2000 字）
    chunks, current = [], ""
    for line in msg.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        await interaction.followup.send(chunk)
        await asyncio.sleep(0.3)

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
