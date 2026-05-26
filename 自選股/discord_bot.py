"""
discord_bot.py
Discord 查股 Bot — 完全雲端運作，電腦離線時照常可用。

運作方式：
  /查股 → Bot 直接抓資料 + Claude 分析 → 結果回覆在同一個頻道
  /自選股 → 觸發 GitHub Actions workflow（需要 GITHUB_PAT）

必要環境變數：
  DISCORD_BOT_TOKEN - Discord Bot Token
  ANTHROPIC_API_KEY - Claude API Key

選填環境變數：
  GITHUB_PAT   - GitHub PAT（workflow 權限），設定後 /自選股 可用
  GITHUB_REPO  - 預設 Kuan-1113/stock-news
"""

import os
import re
import sys
import time
import asyncio
import datetime
import requests
import feedparser
import discord
from discord import app_commands

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from technical_indicators import get_full_indicators, format_indicators_for_prompt

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")

import pytz
TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

# ─────────────────────────────────────────────────────────────
# 分析核心（同步，在 executor 中執行）
# ─────────────────────────────────────────────────────────────

def _claude_call(prompt: str, max_tokens: int = 1400) -> str:
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
                "model": "claude-opus-4-5",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        return f"AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        return f"AI 分析暫時無法使用（{str(e)[:80]}）"

def _fetch_quote(symbol: str) -> dict | None:
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
        if len(closes) < 2:
            return None
        prev, curr = closes[-2], closes[-1]
        chg  = curr - prev
        pct  = chg / prev * 100
        vols = [v for v in volumes if v is not None]
        return {
            "name":     meta.get("shortName", symbol),
            "price":    f"{curr:,.2f}",
            "change":   f"{chg:+.2f}",
            "pct":      f"{pct:+.2f}%",
            "emoji":    "🔴" if pct < 0 else "🟢",
            "currency": meta.get("currency", ""),
            "volume":   f"{vols[-1]:,.0f}" if vols else "N/A",
            "closes":   closes,
        }
    except Exception as e:
        print(f"Yahoo 錯誤：{e}")
        return None

def _fetch_news(symbol: str, name: str) -> str:
    titles, seen = [], set()
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
        stock_no = symbol.replace(".TW", "")
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(name + ' 台股')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        time.sleep(0.2)
        _add("https://news.cnyes.com/rss/cat/tw_stock_news")
        time.sleep(0.2)
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(stock_no)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    else:
        _add(f"https://news.google.com/rss/search?q={requests.utils.quote(symbol + ' stock')}&hl=en-US&gl=US&ceid=US:en")
        time.sleep(0.2)
        _add(f"https://finance.yahoo.com/rss/headline?s={symbol}")
    return "\n".join(titles[:8]) if titles else "（暫無相關新聞）"

def _do_analysis(symbol: str, name: str) -> str:
    """完整查股分析，回傳 Discord 訊息文字"""
    quote = _fetch_quote(symbol)
    if not quote:
        return f"❌ 找不到股票代碼 `{symbol}`\n格式範例：台股 `2330.TW`、美股 `NVDA`、指數 `^TWII`"

    display_name = name or quote.get("name", symbol)
    news = _fetch_news(symbol, display_name)
    ind  = get_full_indicators(symbol)

    ind_text = format_indicators_for_prompt(ind) if ind and ind.get("available") else "（未取得技術指標）"

    closes = quote.get("closes", [])
    price_history = ""
    if len(closes) >= 5:
        price_history = "近5日收盤：" + " → ".join([f"{c:,.2f}" for c in closes[-5:]])

    prompt = f"""你是一位資深股票分析師，請對以下股票進行深度分析。

【股票資訊】
- 名稱：{display_name}（{symbol}）
- 最新價格：{quote['price']} {quote.get('currency', '')}
- 漲跌：{quote['change']} ({quote['pct']})
- {price_history}

【技術指標（實際計算值）】
{ind_text}

【近期相關新聞】
{news}

請以繁體中文撰寫分析報告（900字以內）：

📊 **近期走勢**（2-3句）

🔍 **技術面分析**
- 均線多空排列、MACD 動能、RSI 位置、KDJ 訊號、乖離率、成交量

📰 **基本面亮點**（結合新聞，2-3點）

🎯 **短期預測（1-2週）**
- 目標價區間、可能走向

💡 **操作建議**
- 進場條件、觀望條件、停損參考

⚠️ 本報告由 AI 生成，不構成投資建議。"""

    analysis = _claude_call(prompt)
    header = (
        f"## {quote['emoji']} **{display_name}（{symbol}）** 即時查股 | {now_str()}\n"
        f"💰 **{quote['price']}** {quote.get('currency','')}　"
        f"{quote['change']} ({quote['pct']})\n\n"
    )
    return header + analysis

# ─────────────────────────────────────────────────────────────
# GitHub Actions 觸發（/自選股 用）
# ─────────────────────────────────────────────────────────────

def _trigger_workflow(workflow_file: str, inputs: dict) -> tuple[bool, str]:
    if not GITHUB_PAT:
        return False, "未設定 GITHUB_PAT"
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches",
            headers={
                "Authorization": f"token {GITHUB_PAT}",
                "Accept": "application/vnd.github.v3+json",
            },
            json={"ref": "main", "inputs": inputs},
            timeout=10,
        )
        if r.status_code == 204:
            return True, ""
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:80]

# ─────────────────────────────────────────────────────────────
# Discord Bot
# ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

def _split_messages(text: str, limit: int = 1900) -> list[str]:
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        chunks.append(cur)
    return chunks

@tree.command(name="查股", description="查詢股票即時行情與 AI 深度分析")
@app_commands.describe(
    symbol="股票代碼（台股：2330.TW｜美股：NVDA｜指數：^TWII）",
    name="股票名稱（選填，如：台積電）",
)
async def cmd_查股(interaction: discord.Interaction, symbol: str, name: str = ""):
    print(f"⚡ /查股 收到：{symbol}", flush=True)
    try:
        print(f"⚡ 準備 defer...", flush=True)
        await interaction.response.defer()
        print(f"⚡ defer 成功", flush=True)
        sym = symbol.strip().upper()
        # 在背景執行緒中跑全部同步分析，不阻塞 event loop
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_analysis, sym, name.strip())
        # 分段傳送（Discord 單則訊息上限 2000 字）
        chunks = _split_messages(result)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查股 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass

@tree.command(name="自選股", description="觸發完整自選股分析（結果約 2 分鐘後出現）")
async def cmd_自選股(interaction: discord.Interaction):
    try:
        await interaction.response.defer()
        loop          = asyncio.get_running_loop()
        ok, errmsg    = await loop.run_in_executor(None, _trigger_workflow, "query_watchlist.yml", {})
        if ok:
            await interaction.followup.send("⏳ 自選股分析啟動！約 **2 分鐘**後報告會出現在自選股頻道。")
        elif not GITHUB_PAT:
            await interaction.followup.send(
                "⚠️ 尚未設定 `GITHUB_PAT`，無法觸發自選股分析。\n"
                "請在 Bot 環境變數中加入 `GITHUB_PAT`（需 `workflow` 權限的 GitHub PAT）。"
            )
        else:
            await interaction.followup.send(f"❌ 觸發失敗：`{errmsg}`")
    except Exception as e:
        print(f"❌ /自選股 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

@client.event
async def on_ready():
    await tree.sync()
    cmds = [c.name for c in tree.get_commands()]
    print(f"✅ Bot 啟動：{client.user}（ID: {client.user.id}）")
    print(f"   指令：{cmds}")
    print(f"   ANTHROPIC_API_KEY：{'✅ 已設定' if ANTHROPIC_API_KEY else '❌ 未設定'}")
    print(f"   GITHUB_PAT：{'✅ 已設定（/自選股 可用）' if GITHUB_PAT else '⚠️  未設定（/自選股 不可用）'}")

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動")
else:
    client.run(DISCORD_BOT_TOKEN)
