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
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")

import pytz
TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

def _strip_md_tables(text: str) -> str:
    """移除 AI 偶爾生成的 Markdown 表格行，轉為 bullet 格式"""
    import re as _re
    lines, out = text.split("\n"), []
    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and _re.match(r"^\|[-:\s|]+\|$", s):
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            if cells:
                out.append("• " + "　".join(cells))
            continue
        out.append(line)
    return "\n".join(out)

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
                "model": CLAUDE_MODEL,
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
            "emoji":    "🔴" if pct >= 0 else "🟢",   # 台灣慣例：紅=漲，綠=跌
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

    # ── 第一步：立即 defer（必須在 3 秒內完成，否則互動作廢）──
    try:
        await interaction.response.defer()
        print(f"⚡ defer 成功", flush=True)
    except discord.errors.NotFound:
        # 互動 token 已逾時（3 秒超過），Discord 自動顯示「應用程式沒有回應」
        # 不要再嘗試送任何訊息，靜默略過即可
        print(f"⚠️ /查股 互動已逾時（token 過期），略過：{symbol}")
        return
    except Exception as e:
        print(f"❌ /查股 defer 失敗：{e}")
        return

    # ── 第二步：執行分析並回傳結果 ──
    try:
        sym  = symbol.strip().upper()
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_analysis, sym, name.strip())
        chunks = _split_messages(result)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查股 分析例外：{e}")
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass

@tree.command(name="自選股", description="觸發完整自選股分析（結果約 2 分鐘後出現）")
async def cmd_自選股(interaction: discord.Interaction):
    # ── 第一步：立即 defer ──
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /自選股 互動已逾時，略過")
        return
    except Exception as e:
        print(f"❌ /自選股 defer 失敗：{e}")
        return

    # ── 第二步：觸發 workflow ──
    try:
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
        print(f"❌ /自選股 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 發生錯誤：{str(e)[:100]}")
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
# 期貨試算功能
# ─────────────────────────────────────────────────────────────

_FUT_SPEC = {
    "TX":  {"name": "臺股期貨（大台）",     "yf": "TXF=F",  "mult": 200,  "tick": 1},
    "MX":  {"name": "小型臺股期貨（小台）", "yf": "MXF=F",  "mult": 50,   "tick": 1},
    "TE":  {"name": "電子期貨",             "yf": None,      "mult": 4000, "tick": 0.05},
    "TF":  {"name": "金融期貨",             "yf": None,      "mult": 1000, "tick": 0.2},
    "XIF": {"name": "非金電期貨",           "yf": None,      "mult": 200,  "tick": 1},
    "GDF": {"name": "黃金期貨",             "yf": "GC=F",    "mult": 100,  "tick": 0.5},
    "BRF": {"name": "布蘭特原油期貨",       "yf": "BZ=F",    "mult": 1000, "tick": 0.01},
}
# 原始/維持保證金（TWD，TAIFEX 每季調整，以下為參考值）
_MARGIN_TABLE = {
    "TX":  {"orig": 170000, "maint": 130000},
    "MX":  {"orig":  43000, "maint":  33000},
    "TE":  {"orig":  84000, "maint":  64500},
    "TF":  {"orig":  41000, "maint":  31500},
    "XIF": {"orig":  42000, "maint":  32000},
    "GDF": {"orig":  27000, "maint":  20700},
    "BRF": {"orig":  27000, "maint":  20700},
}

def _fetch_futures_history(yf_symbol: str, days: int = 12) -> list:
    """從 Yahoo Finance 取近 N 日 OHLCV"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}?interval=1d&range={days}d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        if r.status_code != 200:
            return []
        result = r.json()["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        q = result["indicators"]["quote"][0]
        rows = []
        for i, ts in enumerate(timestamps):
            o = q["open"][i] if q["open"][i] else None
            h = q["high"][i] if q["high"][i] else None
            lo = q["low"][i] if q["low"][i] else None
            c = q["close"][i] if q["close"][i] else None
            v = q["volume"][i] if q["volume"] and i < len(q["volume"]) else None
            if c is None:
                continue
            dt = datetime.datetime.fromtimestamp(ts, tz=TW_TZ)
            rows.append({"date": dt.strftime("%m/%d"), "open": o, "high": h, "low": lo, "close": c, "volume": v})
        return rows[-min(days, len(rows)):]
    except Exception as e:
        print(f"期貨歷史資料錯誤：{e}")
        return []

def _do_futures(symbol: str, direction: str, entry_price: float, contracts: int) -> str:
    code = symbol.upper().strip()
    spec = _FUT_SPEC.get(code)
    if not spec:
        # 當作 Yahoo Finance symbol 直接查
        spec = {"name": code, "yf": code if "=" in code else f"{code}=F", "mult": 1, "tick": 1}
    margin = _MARGIN_TABLE.get(code, {"orig": 0, "maint": 0})

    mult    = spec["mult"]
    is_long = "買" in direction
    sign    = 1 if is_long else -1

    history       = _fetch_futures_history(spec["yf"], 12) if spec.get("yf") else []
    current_price = history[-1]["close"] if history else None

    # 近10日統計
    hist10    = history[-10:] if len(history) >= 10 else history
    highs     = [r["high"]  for r in hist10 if r.get("high")]
    lows      = [r["low"]   for r in hist10 if r.get("low")]
    amp_list  = [(r["high"] - r["low"]) / r["low"] * 100
                 for r in hist10 if r.get("high") and r.get("low") and r["low"] > 0]
    period_hi = max(highs) if highs else None
    period_lo = min(lows)  if lows  else None
    avg_amp   = sum(amp_list) / len(amp_list) if amp_list else 0

    # P&L 計算
    pnl            = (current_price - entry_price) * mult * contracts * sign if current_price else None
    pnl_per_pt     = mult * contracts
    orig           = margin["orig"]
    maint          = margin["maint"]
    # 追繳觸發：虧損超過 (orig - maint) × contracts
    call_threshold = (orig - maint) * contracts if orig > 0 else 0
    if call_threshold > 0 and mult > 0:
        call_price = entry_price - call_threshold / (mult * contracts) * sign
    else:
        call_price = None
    add_margin = call_threshold  # 追繳回原始保證金水位

    # 組合輸出
    dir_tag  = "🔴 買進（多）" if is_long else "🟢 賣出（空）"
    pnl_tag  = f"{'🟩 獲利' if (pnl or 0) >= 0 else '🟥 虧損'} {pnl:+,.0f} 元" if pnl is not None else "（目前價格未取得）"
    lines = [
        f"## ⚡ 期貨試算 | {now_str()}",
        f"**{spec['name']}（{code}）** — {dir_tag}　**{contracts} 口**",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "**📋 合約資訊**",
        f"• 進場價格：**{entry_price:,.0f}**",
        f"• 合約乘數：{mult:,} 元/點",
        f"• {contracts} 口每點損益：**{pnl_per_pt:,} 元**",
    ]
    if current_price:
        lines += [
            "",
            "**📊 目前損益**",
            f"• 目前價格：**{current_price:,.0f}**",
            f"• {pnl_tag}（{(current_price - entry_price) * sign:+.0f} 點）",
        ]
    if orig > 0:
        lines += [
            "",
            "**💰 保證金資訊**（參考值，請至 taifex.com.tw 確認最新）",
            f"• 原始保證金（每口）：**{orig:,}** 元　→ {contracts} 口合計 **{orig * contracts:,}** 元",
            f"• 維持保證金（每口）：**{maint:,}** 元　→ {contracts} 口合計 **{maint * contracts:,}** 元",
        ]
    if call_price is not None:
        dir_word = "跌破" if is_long else "突破"
        lines += [
            "",
            "**⚠️ 追加保證金觸發點（Margin Call）**",
            f"• 當價格 {dir_word} **{call_price:,.0f}** 時，需追繳保證金",
            f"• 追繳金額（補回原始水位）：**{add_margin:,} 元**",
        ]
    if hist10:
        lines += [
            "",
            f"**📅 近 {len(hist10)} 個交易日行情**",
        ]
        for row in hist10:
            amp = (row["high"] - row["low"]) / row["low"] * 100 if row.get("low") and row["low"] > 0 else 0
            lines.append(f"• {row['date']}　高 {row['high']:,.0f}　低 {row['low']:,.0f}　收 {row['close']:,.0f}　振幅 {amp:.1f}%")
        lines += [
            "",
            f"• 期間最高：**{period_hi:,.0f}**　最低：**{period_lo:,.0f}**",
            f"• {len(hist10)} 日平均振幅：**{avg_amp:.1f}%**",
        ]

    # AI 分析
    price_ctx = f"目前價 {current_price:,.0f}" if current_price else "（目前價格未取得）"
    pnl_ctx   = f"損益 {pnl:+,.0f} 元" if pnl is not None else ""
    prompt = f"""你是期貨交易風險分析師。以下是試算數據，用繁體中文給出簡短風險評估（250字內）。

商品：{spec['name']}（{code}） / 方向：{direction} / 進場：{entry_price:,} / {contracts}口
{price_ctx}　{pnl_ctx}
原始保證金：{orig:,}　維持：{maint:,}　追繳觸發：{f'{call_price:,.0f}' if call_price else 'N/A'}
近期振幅：{avg_amp:.1f}% / 高：{period_hi:,.0f if period_hi else 'N/A'} / 低：{period_lo:,.0f if period_lo else 'N/A'}

請輸出（不用表格）：
• **當前狀態**：盈虧方向與風險程度
• **關鍵價位**：支撐/壓力（依近期行情）
• **操作建議**：加碼/減碼/停損條件（具體價位）

⚠️ 試算結果，非投資建議。"""
    lines += [
        "",
        "**🤖 AI 風險評估**",
        _strip_md_tables(_claude_call(prompt, max_tokens=400)),
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "> ⚠️ 保證金數字為參考值，請至 [TAIFEX](https://www.taifex.com.tw/cht/2/parame) 查詢最新數字",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 權證查詢功能
# ─────────────────────────────────────────────────────────────

def _fetch_warrants(stock_code: str, w_type: str = "") -> tuple:
    """多源搜尋指定股票的相關權證，回傳 (warrant_rows, news_titles)"""
    warrant_rows = []
    news_titles  = []
    code = stock_code.replace(".TW", "").strip()

    # ── 1. TWSE 上市權證 API ─────────────────────────────
    try:
        params = {"stockNo": code, "response": "json", "startDate": "", "endDate": "",
                  "issuer": "", "exercise": ""}
        if w_type in ("認購", "認售"):
            params["wType"] = "C" if w_type == "認購" else "P"
        r = requests.get(
            "https://www.twse.com.tw/rwd/zh/warrant/singleSearch",
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.twse.com.tw/"},
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            fields = data.get("fields", [])
            rows   = data.get("data",   [])
            for row in rows[:20]:
                entry = {}
                for i, val in enumerate(row):
                    key = fields[i] if i < len(fields) else f"f{i}"
                    entry[key] = val
                warrant_rows.append(entry)
            print(f"  TWSE 權證：{len(warrant_rows)} 筆")
    except Exception as e:
        print(f"  TWSE 權證錯誤：{e}")

    # ── 2. TPEX 上櫃權證 API ─────────────────────────────
    if len(warrant_rows) < 5:
        try:
            r2 = requests.get(
                "https://www.tpex.org.tw/web/regular_emerging/covered_warrant/wr01_s.php",
                params={"l": "zh-tw", "stock_code": code, "d": ""},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if r2.status_code == 200:
                import re as _re
                # 從 HTML 表格提取行
                trs = _re.findall(r"<tr[^>]*>(.*?)</tr>", r2.text, _re.S)
                for tr in trs[1:16]:
                    cells = _re.findall(r"<td[^>]*>(.*?)</td>", tr, _re.S)
                    cells = [_re.sub(r"<[^>]+>", "", c).strip() for c in cells]
                    if len(cells) >= 6 and cells[0]:
                        warrant_rows.append({
                            "權證代號": cells[0], "權證名稱": cells[1],
                            "類型": cells[2],     "履約價": cells[3],
                            "到期日": cells[4],   "現價": cells[5],
                        })
            print(f"  TPEX 補充後共 {len(warrant_rows)} 筆")
        except Exception as e:
            print(f"  TPEX 權證錯誤：{e}")

    # ── 3. 鉅亨網 + Google News 補充新聞 ─────────────────
    for url in [
        f"https://news.google.com/rss/search?q={requests.utils.quote(code + ' 權證 認購 認售')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        f"https://news.google.com/rss/search?q={requests.utils.quote(code + ' 台股 選擇權 期貨')}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
    ]:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:5]:
                t = getattr(e, "title", "").strip()
                if t and t not in news_titles:
                    news_titles.append(t)
            time.sleep(0.2)
        except Exception:
            pass

    return warrant_rows[:20], news_titles[:10]


def _do_warrant_analysis(stock_code: str, warrant_type: str) -> str:
    code    = stock_code.replace(".TW", "").strip()
    quote   = _fetch_quote(f"{code}.TW") or _fetch_quote(stock_code)
    rows, news = _fetch_warrants(code, warrant_type)

    # 格式化權證列表
    warrant_text = ""
    if rows:
        lines = []
        for r in rows[:15]:
            # 嘗試不同 key 格式
            w_code  = r.get("權證代號") or r.get("代號", "?")
            w_name  = r.get("權證名稱") or r.get("名稱", "")
            w_type_ = r.get("類型") or r.get("認購/認售", "")
            w_exer  = r.get("履約價") or r.get("行使比例", "?")
            w_exp   = r.get("到期日") or r.get("最後交易日", "?")
            w_price = r.get("現價") or r.get("收盤價", "?")
            lines.append(f"• {w_code} {w_name}　{w_type_}　履約價 {w_exer}　到期 {w_exp}　現價 {w_price}")
        warrant_text = "\n".join(lines)
    else:
        warrant_text = "（未取得結構化權證列表，請參考新聞）"

    # 新聞
    news_text = "\n".join(f"• {t}" for t in news) if news else "（暫無相關新聞）"

    # 股票行情
    quote_line = ""
    if quote:
        quote_line = f"現股 {code} 現價：{quote['price']}（{quote['pct']}）"

    prompt = f"""你是台灣權證分析師。根據以下資料，以繁體中文給出完整分析（600字內）。
嚴禁表格（| 符號），全程 bullet（• 開頭）。

【標的股票】{code} {quote_line}
【搜尋到的權證（{len(rows)} 筆）】
{warrant_text}
【相關新聞】
{news_text}

請輸出：
**📋 權證概況**（目前有幾檔認購/認售，整體流動性概況）
**💡 權證挑選建議**（哪類更適合目前行情，說明原因：履約價 vs 現價位置、時間價值、delta方向）
**⚠️ 風險提示**（時間耗損、槓桿風險、流動性風險等）
**🔗 查詢來源**：[TWSE 權證搜尋](https://www.twse.com.tw/rwd/zh/warrant/singleSearch) | [TPEX 權證](https://www.tpex.org.tw/web/regular_emerging/covered_warrant/wr01_s.php)

> ⚠️ AI 生成，不構成投資建議。"""

    header = (
        f"## 🎫 **{code} 權證查詢** | {now_str()}\n"
        + (f"📌 {quote_line}\n" if quote_line else "")
        + f"🔍 搜尋類型：{'全部' if not warrant_type else warrant_type}　找到 **{len(rows)}** 筆\n\n"
    )
    if rows:
        header += f"**📑 權證列表**\n{warrant_text}\n\n---\n\n"
    return header + _strip_md_tables(_claude_call(prompt, max_tokens=700))


@tree.command(name="期貨", description="期貨試算：輸入方向、價格、口數 → 損益 / 保證金 / 追繳分析")
@app_commands.describe(
    symbol      = "期貨代碼（TX=大台、MX=小台、TE=電子、TF=金融、GDF=黃金、BRF=布蘭特油）",
    direction   = "買進 或 賣出",
    entry_price = "進場價格（數字）",
    contracts   = "口數（預設 1）",
)
async def cmd_期貨(
    interaction: discord.Interaction,
    symbol:      str,
    direction:   str,
    entry_price: float,
    contracts:   int = 1,
):
    print(f"⚡ /期貨 收到：{symbol} {direction} @{entry_price} {contracts}口", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /期貨 互動已逾時")
        return
    except Exception as e:
        print(f"❌ /期貨 defer 失敗：{e}")
        return
    try:
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _do_futures, symbol.strip(), direction.strip(), entry_price, max(1, contracts)
        )
        for i, chunk in enumerate(_split_messages(result)):
            await (interaction.followup.send if i == 0 else interaction.channel.send)(chunk)
    except Exception as e:
        print(f"❌ /期貨 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 試算失敗：{str(e)[:150]}")
        except Exception:
            pass


@tree.command(name="權證", description="查詢指定股票的相關認購/認售權證清單與分析")
@app_commands.describe(
    stock_code   = "股票代號（台股不含 .TW，如：2330、0050）",
    warrant_type = "認購 / 認售 / 全部（預設全部）",
)
async def cmd_權證(
    interaction:  discord.Interaction,
    stock_code:   str,
    warrant_type: str = "全部",
):
    print(f"⚡ /權證 收到：{stock_code} {warrant_type}", flush=True)
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /權證 互動已逾時")
        return
    except Exception as e:
        print(f"❌ /權證 defer 失敗：{e}")
        return
    try:
        wt = "" if warrant_type in ("全部", "") else warrant_type.strip()
        loop   = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_warrant_analysis, stock_code.strip(), wt)
        for i, chunk in enumerate(_split_messages(result)):
            await (interaction.followup.send if i == 0 else interaction.channel.send)(chunk)
    except Exception as e:
        print(f"❌ /權證 例外：{e}")
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
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
    print(f"   新指令：/期貨（試算損益/保證金/追繳）、/權證（TWSE+TPEX多源搜尋）")

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動")
else:
    client.run(DISCORD_BOT_TOKEN)
