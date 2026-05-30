"""
agents/analyst_agent.py — Claude AI 分析 Agent
台股 / 美股 / 全球 + 金十 + AI新聞 共 5 份分析並行執行

新增（對應 stock_daily.py 功能）：
  - analyze_jin10()：金十快訊專屬分析
  - analyze_ai()：AI 全球動態分析
  - analyze_global()：加入金十文字作為 context
  - analyze_us()：加入 VIP 人物動態
  - _strip_md_tables()：移除 Markdown 表格，避免 Discord 顯示殘缺
"""

import re
import requests
from concurrent.futures import ThreadPoolExecutor

from shared.config import ANTHROPIC_API_KEY
from shared.utils import (
    build_news_text, fmt_quote, now_str, is_weekend
)
from agents.news_agent import build_jin10_text


# ── 工具函式 ─────────────────────────────────────────────────────

def _strip_md_tables(text: str) -> str:
    """把 AI 偶爾生成的 Markdown 表格轉為 bullet，避免 Discord 顯示殘缺"""
    lines  = text.split("\n")
    output = []
    for line in lines:
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and re.match(r"^\|[-:\s|]+\|$", s):
            continue   # 表格分隔行直接跳過
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells:
                output.append("• " + "　".join(cells))
            continue
        output.append(line)
    return "\n".join(output)


# ── Claude API 呼叫 ───────────────────────────────────────────────

def claude_call(prompt: str, max_tokens: int = 1800, model: str = "claude-sonnet-4-6") -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ 未設定 ANTHROPIC_API_KEY，AI 分析無法使用。"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        if r.status_code == 200:
            return _strip_md_tables(r.json()["content"][0]["text"].strip())
        elif r.status_code == 429:
            print(f"⏳ Claude rate limit，稍後重試...")
            return "⏳ AI 分析暫時無法使用（rate limit），請稍後再試。"
        else:
            print(f"❌ Claude API 錯誤：HTTP {r.status_code} {r.text[:200]}")
            return f"⚠️ AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"⚠️ AI 分析暫時無法使用（{str(e)[:100]}）"


# ── 台股分析 ──────────────────────────────────────────────────────

def analyze_tw(news: list, session_info: dict, market_data: dict) -> str:
    twii = market_data.get("twii", {})
    weekend_note = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    news_quality = f"✅ 新聞：共 {len(news)} 則" if news else "❌ 本時段無新聞資料"

    prompt = f"""台股分析師。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌。
不需按固定章節填寫，以邏輯流暢為主，總字數 1100 字以內。

━━━ PHASE 1：資訊收斂 ━━━
✅ [TWSE] 加權指數：{fmt_quote(twii) if twii else 'N/A'}
{news_quality}

━━━ 台股新聞 ━━━
{build_news_text(news)}

━━━ 核心任務（順序自由，篇幅自由調配）━━━

**A. 歸因分析（必做）**
今日台股主要驅動力是什麼？
• 識別 1-3 個主因（指數/族群/消息/外部），說明哪個最主導
• 這個驅動是一日事件還是延續性趨勢？後續是否有跟進空間？
• 若方向不明，列出競爭性解釋並說明觀察哪個指標可確認

**B. 類股輪動與個股（必做）**
• 強勢族群：驅動因素 + 龍頭股代號
• 弱勢族群：壓力來源
• 值得追蹤的個股（3-4檔）：代號 + 事件 + 評估

**C. 多空決策（必做）**
• 多方核心論點（訊號 → 邏輯 → 目標）
• 空方核心論點（訊號 → 邏輯 → 風險位）
• 當前偏向 + 最關鍵決定因素
• 操作建議：觸發條件 → 標的 → 方向 → 停損

**D. 風險提示**（1-2句）

> ⚠️ 以上為 AI 生成分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1600)


# ── 美股分析 ──────────────────────────────────────────────────────

def analyze_us(news: list, session_info: dict, market_data: dict, vip_news: dict = None) -> str:
    dji  = market_data.get("dji",  {})
    ixic = market_data.get("ixic", {})
    gspc = market_data.get("gspc", {})
    weekend_note = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    news_quality = f"✅ 新聞：共 {len(news)} 則" if news else "❌ 本時段無新聞資料"

    # VIP 人物動態
    vip_block = ""
    if vip_news:
        def _titles(key):
            articles = vip_news.get(key) or []
            return "　".join(
                f"[{a['pub_dt'].strftime('%H:%M')}] {a['title'][:50]}" if a.get("pub_dt")
                else a["title"][:50]
                for a in articles[:2]
            ) or "（暫無新聞）"
        vip_block = (
            "\n【重要人物最新動態】\n"
            f"🚀 馬斯克（Musk）：{_titles('musk')}\n"
            f"🏛️ 川普（Trump）：{_titles('trump')}\n"
            f"🟩 黃仁勳（Jensen Huang）：{_titles('jensen')}"
        )

    prompt = f"""美股分析師。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌。
不需按固定章節填寫，以邏輯流暢為主，總字數 1100 字以內。

━━━ PHASE 1：資訊收斂 ━━━
✅ [Yahoo Finance] 道瓊：{fmt_quote(dji) if dji else 'N/A'} ／ 納指：{fmt_quote(ixic) if ixic else 'N/A'} ／ S&P：{fmt_quote(gspc) if gspc else 'N/A'}
{news_quality}
{vip_block}

━━━ 美股新聞 ━━━
{build_news_text(news)}

━━━ 核心任務（順序自由，篇幅自由調配）━━━

**A. 歸因分析（必做）**
今日美股主要驅動力是什麼？
• 識別 1-3 個主因（總經數據/Fed聲明/財報/板塊輪動/地緣政治），說明相對重要性
• 是單次事件還是持續趨勢？後續哪個數據或事件會確認或推翻？

**B. 板塊與個股（必做）**
• 強勢板塊：驅動因素 + 可關注個股
• 弱勢板塊：壓力來源 + 迴避方向
• 重點個股（3-4檔）：代號 + 事件 + 評估

**C. 多空決策（必做）**
• 多方核心論點（訊號 → 邏輯 → 目標位）
• 空方核心論點（訊號 → 邏輯 → 下行風險）
• 當前偏向 + 最關鍵決定因素
• 操作建議：觸發條件 → 標的 → 方向 → 停損

**D. 重要人物動態**（有相關新聞才寫）
• 人物：發言/動作 → 受影響板塊/資產

> ⚠️ 以上為 AI 生成分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1700)


# ── 國際分析 ──────────────────────────────────────────────────────

def analyze_global(news: list, session_info: dict, market_data: dict, jin10_text: str = "") -> str:
    vix   = market_data.get("vix",   {})
    gold  = market_data.get("gold",  {})
    oil   = market_data.get("oil",   {})
    dxy   = market_data.get("dxy",   {})
    us10y = market_data.get("us10y", {})
    crypto = market_data.get("crypto", {})
    btc   = crypto.get("BTC", {})
    eth   = crypto.get("ETH", {})
    weekend_note = "\n⚠️ 今日為週末，部分指標為上次交易日數值。" if is_weekend() else ""
    news_quality = f"✅ 新聞：共 {len(news)} 則" if news else "❌ 本時段無新聞資料"

    def _fmt_crypto(d: dict) -> str:
        if not d:
            return "N/A"
        return f"{d.get('emoji','')} {d.get('price','N/A')} ({d.get('pct','N/A')})"

    prompt = f"""國際財經分析師，採用三階段迭代分析框架。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）
每個論點格式：「事件/數據（數值）→ 傳導機制 → 影響方向」。禁空話，禁 Markdown 表格，全程 bullet（•）。台灣慣例：🔴=漲/利多，🟢=跌/利空。

━━━ PHASE 1：資訊收斂確認 ━━━
✅ [Yahoo Finance] VIX：{fmt_quote(vix) if vix else 'N/A'} ／ 美債10Y：{fmt_quote(us10y) if us10y else 'N/A'} ／ DXY：{fmt_quote(dxy) if dxy else 'N/A'}
✅ [Yahoo Finance] 黃金：{fmt_quote(gold) if gold else 'N/A'} ／ 原油：{fmt_quote(oil) if oil else 'N/A'}
✅ [CoinGecko] BTC：{_fmt_crypto(btc)} ／ ETH：{_fmt_crypto(eth)}
{news_quality}
{'✅ [金十數據] 快訊已附入' if jin10_text else '⚠️ 金十數據：未取得（JIN10_TOKEN 未設定）'}

━━━ 國際新聞 ━━━
{build_news_text(news)}
{jin10_text if jin10_text else ''}

━━━ 核心任務（900字以內，順序自由，篇幅自由調配）━━━

**A. 全球歸因分析（必做）**
全球市場今日的宏觀主軸是什麼？
• VIX/美債/DXY 數值說明什麼？Risk-On 還是 Risk-Off？為什麼？
• 識別 1-3 個最主要驅動事件，說明傳導路徑

**B. 重大事件影響（必做）**
• 事件（數值）→ 傳導路徑 → 🔴利多/🟢利空（高/中/低）

**C. 大宗商品與加密**
• 品項：漲跌 → 驅動因素 → 對通膨/央行政策的隱含訊號

**D. 多空決策（必做）**
• 多方力量（1-2個支撐因素）→ 受惠資產/族群
• 空方風險（1-2個壓制因素）→ 受壓資產/族群
• 對台股的具體傳導路徑：哪些族群（代號）受惠或受壓，為什麼？

**E. 本週重要財經事件**（若有）
• 數據名稱：預期值 → 若超預期/不如預期，影響方向

> ⚠️ 以上為 AI 生成分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1600)


# ── 金十專屬分析 ──────────────────────────────────────────────────

def analyze_jin10(flash: list, calendar: list, session_info: dict) -> str:
    """針對金十數據進行專屬 AI 分析"""
    if not flash and not calendar:
        return ""
    jin10_text = build_jin10_text(flash, calendar)
    prompt = f"""國際財經分析師。根據以下金十數據近24小時快訊與行事曆，以繁體中文撰寫完整分析（700字內）。
嚴禁表格，全程 bullet（• 開頭）。台灣慣例：🔴=漲/利多，🟢=跌/利空。
每個論點格式：「具體事件/數值 → 傳導機制 → 影響方向與程度」

{jin10_text}

以下四節全部完整輸出：

**🔥 最重大事件因果分析**（3-5個）
• 事件（數值）→ 影響資產 → 🔴利多/🟢利空 → 市場已反應或尚未反應

**⚔️ 全球多空格局**
多方力量：（數據/事件支撐上漲的2個最強理由）
空方風險：（數據/事件可能壓制市場的2個最大威脅）
當前偏向：[多/空/均衡] → 最關鍵因素一句

**🇹🇼 台股/亞股影響路徑**
• 全球因素X → 傳導到台灣的路徑 → 受惠族群（代號）或受壓族群

**📅 重要財經數據**
• 數據名稱：預期值 → 若高於/低於預期，影響方向（升/降/多/空）"""
    return claude_call(prompt, max_tokens=1000)


# ── AI 新聞分析 ────────────────────────────────────────────────────

def analyze_ai(articles: list, session_info: dict) -> str:
    """AI & AI Agent 全球動態分析"""
    if not articles:
        return ""
    news_text = build_news_text(articles)
    prompt = f"""AI科技趨勢分析師。{now_str()}，「{session_info['label']}」。
【AI全球動態】
{news_text}

以繁體中文寫（700字內），必帶具體公司/產品名稱，禁泛說「AI持續進步」，禁用 Markdown 表格，全程 bullet（•）。
每個論點格式：「具體事件（公司/產品）→ 技術/商業意義 → 對產業鏈影響 → 台股受惠/受壓方向」

**🚀 重大突破/新發布**（1-3個）
• 公司/產品：做了什麼（具體功能/參數）→ 技術意義 → 誰是直接受益者

**⚔️ AI賽局多空論點**
利多方向（AI投資加速的理由＋哪些公司/族群直接受惠）：
•
利空方向（競爭加劇/替代效應/監管風險 → 哪些公司/族群受壓）：
•

**📈 台灣科技股影響路徑**（必須完整輸出，2-3檔）
• 台股代號 公司名：全球AI事件X → 訂單/技術/競爭的傳導路徑 → 股價偏多/偏空/觀望

> ⚠️ AI 生成，不構成投資建議。"""
    return claude_call(prompt, max_tokens=1400)


# ── Agent ─────────────────────────────────────────────────────────

class AnalystAgent:
    """
    並行執行台股 / 美股 / 全球 / 金十 / AI 共 5 份分析
    回傳：
    {
      "tw": "...",
      "us": "...",
      "global": "...",
      "jin10": "...",   ← 金十專屬分析（無 Token 時為空字串）
      "ai": "...",      ← AI 全球動態分析
    }
    """

    def run(self, market_data: dict, news_data: dict, session_info: dict) -> dict:
        print("🤖 [AnalystAgent] 並行分析中（台股 / 美股 / 全球 / 金十 / AI）...")

        jin10_flash    = news_data.get("jin10_flash",    [])
        jin10_calendar = news_data.get("jin10_calendar", [])
        jin10_text     = build_jin10_text(jin10_flash, jin10_calendar)
        vip_news       = news_data.get("vip", {})
        ai_news        = news_data.get("ai",  [])

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_tw     = executor.submit(
                analyze_tw,
                news_data.get("tw", []), session_info, market_data,
            )
            future_us     = executor.submit(
                analyze_us,
                news_data.get("us", []), session_info, market_data, vip_news,
            )
            future_global = executor.submit(
                analyze_global,
                news_data.get("global", []), session_info, market_data, jin10_text,
            )
            future_jin10  = executor.submit(
                analyze_jin10,
                jin10_flash, jin10_calendar, session_info,
            )
            future_ai     = executor.submit(
                analyze_ai,
                ai_news, session_info,
            )

            tw_result     = future_tw.result()
            us_result     = future_us.result()
            global_result = future_global.result()
            jin10_result  = future_jin10.result()
            ai_result     = future_ai.result()

        print("🤖 [AnalystAgent] 完成")
        return {
            "tw":     tw_result,
            "us":     us_result,
            "global": global_result,
            "jin10":  jin10_result,
            "ai":     ai_result,
        }
