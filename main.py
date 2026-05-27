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
from warrant import search_warrants, analyze_warrant, lookup_stock, prewarm_cache
from warrant import _fetch_mis, _ratio
import warrant_db

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_PAT        = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPO", "Kuan-1113/stock-news")
# 查詢指令限制頻道（只有此頻道可使用 /查股 /查權證 /分析權證）
# 設定方法：Discord 開啟開發者模式 → 右鍵頻道 → 複製頻道 ID → 填入 QUERY_CHANNEL_ID
# 留空 = 所有頻道都開放查詢
QUERY_CHANNEL_ID  = os.environ.get("QUERY_CHANNEL_ID", "")
# Claude 模型名稱（可透過環境變數覆寫，避免 Anthropic 改版時需動程式碼）
CLAUDE_MODEL      = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

import pytz
TW_TZ = pytz.timezone("Asia/Taipei")

def now_str():
    return datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

# ── 分析核心 ──────────────────────────────────────────────────

def _claude_call(prompt: str, max_tokens: int = 1600) -> str:
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
            timeout=120,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        err_detail = r.text[:300] if r.text else "(no body)"
        print(f"❌ Claude API {r.status_code}：{err_detail}", flush=True)
        return f"AI 分析暫時無法使用（HTTP {r.status_code}）\n`{err_detail[:150]}`"
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
    prompt = f"""你是一位資深股票分析師，請根據以下真實數據撰寫分析報告。

【股票基本資訊】
代碼：{symbol}　名稱：{display_name}
現價：{quote['price']} {quote.get('currency', '')}　漲跌：{quote['change']}（{quote['pct']}）
{price_history}

【技術指標（實際計算值）】
{ind_text}

【近期相關新聞】
{news}

---
請以繁體中文撰寫，總字數控制在 800 字內，格式如下：

**📊 近期走勢**
（2-3句概述最近行情與關鍵變化，直接點出漲跌原因）

**🔍 技術面解析**
• 均線：多空排列現況與趨勢方向
• MACD：動能方向，是否出現背離或交叉訊號
• RSI / KD：目前位置，是否超買或超賣
• 成交量：量能是否配合行情

**📰 消息面重點**
（條列 1-3 則與股價最相關的新聞，說明對後市的潛在影響）

**🎯 短期展望（1-2 週）**
• 多方情境：目標價位 ／ 觸發上漲的關鍵條件
• 空方情境：關鍵支撐位 ／ 可能下跌的風險因素

**💡 操作建議**
• 積極策略：建議進場時機 ／ 目標價 ／ 停損參考
• 保守策略：觀望條件與等待訊號

> ⚠️ 本報告由 AI 生成，僅供參考，不構成任何投資建議。"""
    analysis = _claude_call(prompt, max_tokens=1600)
    header = (
        f"## {quote['emoji']} **{display_name}（{symbol}）** | {now_str()}\n"
        f"**現價：{quote['price']} {quote.get('currency','')}**　"
        f"漲跌：{quote['change']}（{quote['pct']}）\n"
        f"{'─' * 28}\n\n"
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

def _update_warrant_prices_bg():
    """
    每日收盤後（16:30）批量更新曾被查詢的權證收盤價並計算 BS。
    使用批量 MIS 大幅降低 HTTP 請求數。
    """
    from warrant import _fetch_basic_all, _fetch_basic_all_tpex, _fetch_mis_batch
    from warrant import _ratio as _get_ratio, _strike, _expiry, _wtype, _days_to_expiry
    from warrant_bs import full_calc
    tracked = warrant_db.get_tracked_warrants(limit=300)
    if not tracked:
        print("⏰ 收盤更新：無追蹤權證", flush=True)
        return

    basic_all = _fetch_basic_all() + _fetch_basic_all_tpex()
    basic_map = {r.get("權證代號", ""): r for r in basic_all}

    # 收集所有需查詢的代號（權證 + 標的）
    warrant_pairs: list[tuple[str, str]] = []
    stock_pairs:   list[tuple[str, str]] = []
    seen_stocks: set = set()
    for item in tracked:
        code     = item["code"]
        und_code = item["und_code"]
        row      = basic_map.get(code, {})
        exchange = "otc" if row.get("_source") == "tpex" else "tw"
        warrant_pairs.append((code, exchange))
        if und_code and und_code not in seen_stocks:
            seen_stocks.add(und_code)
            stock_pairs.append((und_code, "tw"))

    print(f"⏰ 批量更新收盤價：{len(tracked)} 檔（批量 MIS）…", flush=True)
    mis_w_map = _fetch_mis_batch(warrant_pairs)
    mis_s_map = _fetch_mis_batch(stock_pairs)

    ok_cnt = bs_cnt = fail_cnt = 0
    for item in tracked:
        code     = item["code"]
        und_code = item["und_code"]
        ratio_f  = item["ratio_f"] or 1.0
        try:
            mis_w = mis_w_map.get(code, {})
            mis_s = mis_s_map.get(und_code, {}) if und_code else {}
            w_p = float(mis_w.get("price") or 0)
            s_p = float(mis_s.get("price") or 0)
            if w_p <= 0:
                fail_cnt += 1
                continue

            # 計算 BS 並存入快取
            bs_rec: dict = {}
            row = basic_map.get(code, {})
            if s_p > 0 and row:
                try:
                    strike_s = _strike(row)
                    days_r   = _days_to_expiry(_expiry(row))
                    is_call  = "購" in _wtype(row)
                    if strike_s and days_r > 0:
                        bs_rec = full_calc(s_p, float(strike_s), days_r, w_p, ratio_f, is_call)
                        if bs_rec:
                            warrant_db.save_bs_cache(code, bs_rec)
                            bs_cnt += 1
                except Exception:
                    pass

            warrant_db.record_price(
                code, und_code, w_p, s_p, ratio_f,
                iv_bs=bs_rec.get("iv"), delta_bs=bs_rec.get("delta"),
                premium_pct=bs_rec.get("premium_pct"),
            )
            ok_cnt += 1
        except Exception:
            fail_cnt += 1

    print(f"⏰ 收盤更新完成：價格 {ok_cnt} / BS快取 {bs_cnt} / 失敗 {fail_cnt}", flush=True)


def _daily_scheduler():
    """在 TW 08:00 / 15:00 / 22:00 觸發 GitHub Actions 日報；16:30 更新權證收盤價"""
    _triggered   = set()
    _trigger_ts  = {}   # key → datetime，防止重啟後雙實例在 10 分鐘內重複觸發
    DAILY_HOURS   = {8: "morning", 15: "afternoon", 22: "evening"}
    PODCAST_HOURS = {9, 21}

    # ── 啟動保護：隨機等待 0-8 秒，避免重啟時兩個實例同時觸發 ──
    import random
    time.sleep(random.uniform(0, 8))

    while True:
        try:
            now = datetime.datetime.now(TW_TZ)
            key = f"{now.strftime('%Y-%m-%d')}-{now.hour}"

            if now.minute < 5 and key not in _triggered:
                # 額外保護：同一 key 10 分鐘內不重複觸發（防重啟雙實例）
                last_ts = _trigger_ts.get(key)
                too_soon = last_ts and (now - last_ts).total_seconds() < 600

                if not too_soon:
                    if now.hour in DAILY_HOURS and GITHUB_PAT:
                        ok, err = _trigger_workflow("stock-daily.yml", {})
                        status  = '✅' if ok else f'❌ {err}'
                        print(f"⏰ 觸發{DAILY_HOURS[now.hour]}日報 {status}", flush=True)
                        _triggered.add(key)
                        if ok:
                            _trigger_ts[key] = now
                    elif now.hour in PODCAST_HOURS and GITHUB_PAT:
                        ok, err = _trigger_workflow("podcast.yml", {})
                        status  = '✅' if ok else f'❌ {err}'
                        print(f"⏰ 觸發Podcast追蹤 {status}", flush=True)
                        _triggered.add(key)
                        if ok:
                            _trigger_ts[key] = now
                else:
                    print(f"⏰ 排程：{key} 10分鐘內已觸發，跳過（防重啟雙送）", flush=True)
                    _triggered.add(key)

            # 16:30 收盤後：批量更新價格 + 同步生命週期狀態
            price_key = f"price-{now.strftime('%Y-%m-%d')}"
            if now.hour == 16 and 30 <= now.minute < 35 and price_key not in _triggered:
                _triggered.add(price_key)
                threading.Thread(target=_update_warrant_prices_bg, daemon=True).start()
                threading.Thread(target=warrant_db.sync_warrant_status, daemon=True).start()

            # 每天 00:00 清除已觸發記錄
            if now.hour == 0 and now.minute == 0:
                _triggered.clear()
                _trigger_ts.clear()
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

def _is_query_channel(interaction: discord.Interaction) -> bool:
    """檢查是否在允許查詢的頻道（未設定 QUERY_CHANNEL_ID 則允許全部）"""
    if not QUERY_CHANNEL_ID:
        return True
    return str(interaction.channel_id) == QUERY_CHANNEL_ID

@tree.command(name="查股", description="查詢股票即時行情與 AI 深度分析")
@app_commands.describe(
    symbol="股票代碼（台股：2330.TW｜美股：NVDA｜指數：^TWII）",
    name="股票名稱（選填，如：台積電）",
)
async def cmd_查股(interaction: discord.Interaction, symbol: str, name: str = ""):
    print(f"⚡ /查股 收到：{symbol}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /查股。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /查股 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /查股 defer 失敗：{e}", flush=True)
        return
    try:
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
    except discord.errors.NotFound:
        print("⚠️ /自選股 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /自選股 defer 失敗：{e}", flush=True)
        return
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

    prompt = f"""你是一位台灣認購權證專家。以下是 {zh_name}（{code}）目前市場上的認購權證候選清單，已依綜合評分由高到低排序，全部剩餘天數 > 90 天。
請直接引用各權證的實際數值，不要泛泛而談。

【認購權證候選清單】
{summary}

請以繁體中文撰寫分析報告（750字以內），格式如下：

**🏆 推薦排名分析**
對每一檔逐一說明，直接帶入實際數值：

**第 N 名　代號 ／ 名稱**（若有【評級】請顯示）
> 剩 X 天　溢價 X%　槓桿 X 倍　今日成交 XXX 張
- ✅ 優勢：（最吸引人的 2 個具體理由，帶數值）
- ⚠️ 注意：（1-2 個風險點，帶數值）

**💡 綜合建議**
• **首選**：第幾名？原因（綜合到期時間、溢價率、流動性、槓桿）
• **短線操作（1-2 週）**：推薦哪檔？理由
• **中線操作（1 個月以上）**：推薦哪檔？理由
• **進場提示**：進場前應確認的關鍵條件（溢價率、流動性、停損設置）

> ⚠️ AI 生成，不構成投資建議。實際交易前請自行確認最新報價。"""

    analysis = _claude_call(prompt, max_tokens=2000)
    header = (
        f"## 🔍 {zh_name}（{code}）認購權證推薦 | {now_str()}\n"
        f"{'─' * 28}\n\n"
    )
    return header + analysis


def _do_warrant_analyze(warrant_code: str) -> str:
    target_summary, similar_summary = analyze_warrant(warrant_code)
    if target_summary.startswith("❌"):
        return target_summary

    prompt = f"""你是一位台灣認購權證專家，請根據以下實際數據進行逐項評估。

【目標權證資料】
{target_summary}

【同標的其他認購權證（供比較）】
{similar_summary}

---
**撰寫規則（必須遵守）：**
- 每項評估格式：「實際數值 → 評估結論（好／普通／差 + 一句理由）」
- 有數值就帶入，不可省略；僅資料顯示「—」才寫「資料不足」
- 禁止只給範圍說明而不帶本權證的實際數值

請以繁體中文撰寫（850字以內）：

**📋 逐項數據評估**

**① 剩餘天數**
到期日【填入】，距今【填入】天 → 評估時間充裕度與時間價值衰退風險

**② 價內外狀態**
履約價【填入】元 vs 標的現價【填入】元 → 目前價內 X% ／ 價外 X%，需要標的漲 X% 才獲利

**③ 溢價率**
溢價率【填入】%（若無法計算請說明原因） → 合理範圍 3-8%；偏高代表需更多漲幅才能回本

**④ 槓桿倍數**
槓桿【填入】倍（若為估算請標注） → 5-15 倍為甜蜜點；評估報酬風險比

**⑤ 流動性（成交量）**
今日成交【填入】張 → 佳（>50 萬張）／普通（5-50 萬張）／差（<5 萬張）；說明出場難易度

**⑥ 發行規模**
發行量【填入】仟單位 → 評估長期供應充足度

> ⚠️ Delta ／ IV ／ 有效槓桿：TWSE 公開資料未提供，需向發行商（元大、凱基、統一等）查詢報價系統

**✅ 主要優勢**（2-3 點，每點帶實際數值）

**⚠️ 主要風險**（2-3 點，每點帶實際數值）

**🔄 同標的選項比較**
（若有其他同標的認購權證）列出簡表：代號 ｜ 到期日 ｜ 剩餘天數 ｜ 溢價率 ｜ 成交張數，並說明各檔優劣
（若無）直接說明「目前市場無其他同標的認購權證可供比較」，**不要製作空表格**

**⭐ 綜合評級**
若資料顯示了【評級 A+/A/B/C/D】，直接引用並說明評定依據；否則根據以上數據自行評定 A／B／C／D，並一句話說明理由。

> ⚠️ AI 生成，不構成投資建議。"""

    analysis = _claude_call(prompt, max_tokens=2200)
    header = (
        f"## 🎯 權證 `{warrant_code.upper()}` 深度分析 | {now_str()}\n"
        f"{'─' * 28}\n\n"
    )
    return header + analysis


@tree.command(name="查權證", description="輸入股票代碼或名稱，列出推薦認購權證（含排名與優缺點）")
@app_commands.describe(stock="股票代碼或中文名（例：2330 或 台積電）")
async def cmd_查權證(interaction: discord.Interaction, stock: str):
    print(f"⚡ /查權證 收到：{stock}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /查權證。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /查權證 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /查權證 defer 失敗：{e}", flush=True)
        return
    try:
        loop         = asyncio.get_running_loop()
        progress_msg = await interaction.followup.send("🔍 正在蒐集資料，請稍候…")
        result       = await loop.run_in_executor(None, _do_warrant_search, stock.strip())
        chunks       = _split_messages(result)
        await progress_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /查權證 執行失敗：{e}", flush=True)
        try:
            await interaction.followup.send(f"❌ 查詢失敗：{str(e)[:150]}")
        except Exception:
            pass


@tree.command(name="分析權證", description="輸入權證代號，深度分析並推薦同標的替代選項")
@app_commands.describe(code="權證代號（例：038542）")
async def cmd_分析權證(interaction: discord.Interaction, code: str):
    print(f"⚡ /分析權證 收到：{code}", flush=True)
    if not _is_query_channel(interaction):
        await interaction.response.send_message(
            "⚠️ 此頻道不開放查詢功能，請前往指定頻道使用 /分析權證。", ephemeral=True)
        return
    try:
        await interaction.response.defer()
    except discord.errors.NotFound:
        print("⚠️ /分析權證 互動已過期（10062），忽略", flush=True)
        return
    except Exception as e:
        print(f"❌ /分析權證 defer 失敗：{e}", flush=True)
        return
    try:
        loop         = asyncio.get_running_loop()
        progress_msg = await interaction.followup.send("🔍 正在分析中，請稍候…")
        result       = await loop.run_in_executor(None, _do_warrant_analyze, code.strip())
        chunks       = _split_messages(result)
        await progress_msg.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.channel.send(chunk)
    except Exception as e:
        print(f"❌ /分析權證 執行失敗：{e}", flush=True)
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
    warrant_db.init_db()   # 建立/確認資料庫表格
    prewarm_cache()        # 背景預熱快取（優先用 DB，DB 過期才打 API）

if not DISCORD_BOT_TOKEN:
    print("❌ 未設定 DISCORD_BOT_TOKEN，Bot 無法啟動", flush=True)
else:
    client.run(DISCORD_BOT_TOKEN)
