import os
import re
import sys
import time
import threading
import asyncio
import datetime
import requests
import feedparser
import discord
from discord import app_commands
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Health check server for Railway PORT binding ──────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

def _start_health():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()

threading.Thread(target=_start_health, daemon=True).start()
print("=== Discord Bot starting ===", flush=True)

# ── local imports ─────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technical_indicators import get_full_indicators, format_indicators_for_prompt
from warrant import search_warrants, analyze_warrant, lookup_stock

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")

import pytz
TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

# ── 分析核心 ──────────────────────────────────────────────────

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
                "model": "claude-haiku-4-5-20251001",
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
        volumes = result["indicators"]["quote"][0].get("volume", [])
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
        print(f"Yahoo 錯誤：{e}", flush=True)
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

# ── 定時觸發日報（Railway 永遠在線，取代不可靠的 GitHub cron）────

def _daily_scheduler():
    """在 TW 08:00 / 15:00 / 22:00 觸發 GitHub Actions 日報"""
    _triggered = set()
    # 日報：08:00 / 15:00 / 22:00 TW
    # Podcast：09:00 / 21:00 TW
    DAILY_HOURS   = {8: "morning", 15: "afternoon", 22: "evening"}
    PODCAST_HOURS = {9, 21}
    while True:
        try:
            now  = datetime.datetime.now(TW_TZ)
            key  = f"{now.strftime('%Y-%m-%d')}-{now.hour}"
            if now.minute < 5 and key not in _triggered:
                if now.hour in DAILY_HOURS and GITHUB_PAT:
                    ok, err = _trigger_workflow("stock-daily.yml", {})
                    print(f"⏰ 觸發{DAILY_HOURS[now.hour]}日報 {'✅' if ok else f'❌ {err}'}", flush=True)
                    _triggered.add(key)
                elif now.hour in PODCAST_HOURS and GITHUB_PAT:
                    ok, err = _trigger_workflow("podcast.yml", {})
                    print(f"⏰ 觸發Podcast追蹤 {'✅' if ok else f'❌ {err}'}", flush=True)
                    _triggered.add(key)
            # 每天 00:00 清除已觸發記錄
            if now.hour == 0 and now.minute == 0:
                _triggered.clear()
        except Exception as e:
            print(f"⚠️ 排程例外：{e}", flush=True)
        time.sleep(30)

threading.Thread(target=_daily_scheduler, daemon=True).start()
print("⏰ 排程啟動（日報 08/15/22:00、Podcast 09/21:00 TW）", flush=True)

# ── Discord Bot ───────────────────────────────────────────────

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
        await interaction.response.defer()
        print(f"⚡ defer 成功", flush=True)
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_analysis, symbol.strip().upper(), name.strip())
        chunks = _split_messages(result)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查股 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass

@tree.command(name="自選股", description="觸發完整自選股分析（結果約 2 分鐘後出現）")
async def cmd_自選股(interaction: discord.Interaction):
    print("⚡ /自選股 收到", flush=True)
    try:
        await interaction.response.defer()
        loop       = asyncio.get_running_loop()
        ok, errmsg = await loop.run_in_executor(None, _trigger_workflow, "query_watchlist.yml", {})
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
        print(f"❌ /自選股 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

# ── 權證指令 ─────────────────────────────────────────────────

def _do_warrant_search(stock_input: str) -> str:
    code, zh_name = lookup_stock(stock_input)
    summary = search_warrants(stock_input)
    if summary.startswith("❌"):
        return summary

    prompt = f"""你是一位台灣權證專家，請根據以下資料為投資人提供分析建議。

【標的股票】{zh_name}（{code}）

【候選認購權證清單（已依綜合評分排序）】
{summary}

請以繁體中文撰寫分析（700字以內），格式如下：

🏆 **推薦排名**
對每一檔權證說明：
- 名稱、代號
- ✅ 優點（2-3點）
- ⚠️ 缺點或注意（1-2點）

💡 **操作建議**
哪一檔最值得優先考慮？說明理由（剩餘天數、溢價率、流動性、槓桿等角度）。

⚠️ 本分析由 AI 生成，不構成投資建議，請自行評估風險。"""

    analysis = _claude_call(prompt, max_tokens=1200)
    header = f"## 🔍 {zh_name}（{code}）認購權證推薦 | {now_str()}\n\n"
    return header + analysis


def _do_warrant_analyze(warrant_code: str) -> str:
    target_summary, similar_summary = analyze_warrant(warrant_code)
    if target_summary.startswith("❌"):
        return target_summary

    prompt = f"""你是一位台灣權證專家，請對以下權證進行深度分析。

【目標權證】
{target_summary}

【同標的其他推薦認購權證】
{similar_summary}

請以繁體中文撰寫分析（700字以內），格式如下：

📋 **權證基本評估**
- 到期時間是否充裕？
- Delta 值的涵義與影響
- 隱含波動率（IV）是否偏高？
- 溢價率是否合理？
- 實際槓桿倍數評估
- 流動性（成交張數）評估

✅ **優點**（2-3點）

⚠️ **缺點 / 注意事項**（2-3點）

🔄 **同標的推薦替代選項**
比較上方3檔，說明各自優缺點及推薦順序。

⚠️ 本分析由 AI 生成，不構成投資建議，請自行評估風險。"""

    analysis = _claude_call(prompt, max_tokens=1200)
    header = f"## 🎯 權證 `{warrant_code.upper()}` 深度分析 | {now_str()}\n\n"
    return header + analysis


@tree.command(name="查權證", description="輸入股票代碼或名稱，列出推薦認購權證（含排名與優缺點）")
@app_commands.describe(stock="股票代碼或中文名（例：2330 或 台積電）")
async def cmd_查權證(interaction: discord.Interaction, stock: str):
    print(f"⚡ /查權證 收到：{stock}", flush=True)
    try:
        await interaction.response.defer()
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_warrant_search, stock.strip())
        for i, chunk in enumerate(_split_messages(result)):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查權證 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass


@tree.command(name="分析權證", description="輸入權證代號，深度分析並推薦同標的替代選項")
@app_commands.describe(code="權證代號（例：038542）")
async def cmd_分析權證(interaction: discord.Interaction, code: str):
    print(f"⚡ /分析權證 收到：{code}", flush=True)
    try:
        await interaction.response.defer()
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_warrant_analyze, code.strip())
        for i, chunk in enumerate(_split_messages(result)):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /分析權證 例外：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 分析失敗：{str(e)[:150]}")
        except Exception:
            pass


@client.event
async def on_ready():
    await tree.sync()
    cmds = [c.name for c in tree.get_commands()]
    print(f"✅ Bot 啟動：{client.user}（ID: {client.user.id}）", flush=True)
    print(f"   指令：{cmds}", flush=True)
    print(f"   ANTHROPIC_API_KEY：{'✅ 已設定' if ANTHROPIC_API_KEY else '❌ 未設定'}", flush=True)
    print(f"   GITHUB_PAT：{'✅ 已設定' if GITHUB_PAT else '⚠️  未設定（/自選股 不可用）'}", flush=True)

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動", flush=True)
else:
    client.run(DISCORD_BOT_TOKEN)
