"""
agents/analyst_agent.py — Claude AI 分析 Agent
台股 / 美股 / 全球 + 金十 + AI新聞 共 5 份分析並行執行

新增（對應 stock_daily.py 功能）：
  - analyze_jin10()：金十快訊專屬分析
  - analyze_ai()：AI 全球動態分析
  - analyze_global()：加入金十文字作為 context
  - analyze_us()：加入 VIP 人物動態
  - _strip_md_tables()：移除 Markdown 表格，避免 Discord 顯示殘缺

SDK 升級（Managed Agents）：
  claude_call() → 依 USE_MANAGED_AGENTS 旗標自動選擇：
    - simple_call()：Messages API（快速、預設）
    - agent_call()：Managed Agents Sessions API（設 USE_MANAGED_AGENTS=1）
"""

import re
from concurrent.futures import ThreadPoolExecutor

from shared.utils import (
    build_news_text, fmt_quote, now_str, is_weekend
)
from agents.news_agent import build_jin10_text
from agents.market_data_agent import format_tech_for_prompt
from shared.claude_client import simple_call, agent_call, USE_MANAGED_AGENTS


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


# ── Claude 呼叫封裝 ───────────────────────────────────────────────

# 各分析類型的 Managed Agent 名稱與系統提示（只有 USE_MANAGED_AGENTS=1 時使用）
_AGENT_SYSTEM = (
    "你是一位資深股票分析師。以繁體中文撰寫深度分析報告。"
    "禁用 Markdown 表格（禁止 | 符號製表）。"
    "全程使用 bullet（• 開頭）條列重點。"
    "台灣慣例：🔴=漲/利多，🟢=跌/利空。"
)


def claude_call(
    prompt: str,
    max_tokens: int = 1800,
    model: str = "claude-sonnet-4-6",
    agent_name: str = "stock-analyst",
) -> str:
    """
    Claude 分析呼叫統一入口。
    • USE_MANAGED_AGENTS=1 → Managed Agents Sessions API（有 Agent 定義快取）
    • 未設定（預設）       → Messages API（直接快速呼叫）
    兩者均自動去除 Markdown 表格，確保 Discord 顯示正常。
    """
    if USE_MANAGED_AGENTS:
        result = agent_call(
            prompt,
            agent_name=agent_name,
            system=_AGENT_SYSTEM,
            model=model,
            max_tokens=max_tokens,
        )
    else:
        result = simple_call(prompt, max_tokens, model)
    return _strip_md_tables(result)


# ── 台股分析 ──────────────────────────────────────────────────────

def analyze_tw(news: list, session_info: dict, market_data: dict) -> str:
    twii = market_data.get("twii", {})
    weekend_note = "\n⚠️ 今日為週末，指數數據為上次交易日收盤價，僅供參考。" if is_weekend() else ""
    news_quality = f"✅ 新聞：共 {len(news)} 則" if news else "❌ 本時段無新聞資料"

    # 取 TWII 技術指標（fetch_yahoo_extended 已計算好）
    twii_tech = twii.get("tech", {})
    tech_text = format_tech_for_prompt(twii_tech)

    prompt = f"""你是一位資深台股分析師。請根據以下數據與新聞，以繁體中文撰寫台股分析。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）

【注意】：直接開始寫分析內容，禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌，總字數 1100 字以內。

【市場數據】
• 加權指數：{fmt_quote(twii) if twii else 'N/A'}
• 新聞筆數：{news_quality}

【加權指數技術指標（^TWII）】
{tech_text}

【本時段台股新聞】
{build_news_text(news)}

━━━ 核心任務（順序自由，篇幅自由調配）━━━

**A. 歸因分析（必做）**
今日台股主要驅動力是什麼？
• 識別 1-3 個主因（指數/族群/消息/外部），說明哪個最主導
• 結合技術指標（均線位置、MACD、RSI）說明目前趨勢結構
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
    return claude_call(prompt, max_tokens=2500)


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

    prompt = f"""你是一位資深美股分析師。請根據以下數據與新聞，以繁體中文撰寫美股分析。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）

【注意】：直接開始寫分析內容，禁空話，禁 Markdown 表格，全程 bullet（•），台灣慣例：🔴=漲，🟢=跌，總字數 1100 字以內。

【市場數據】
• 道瓊：{fmt_quote(dji) if dji else 'N/A'} ／ 納指：{fmt_quote(ixic) if ixic else 'N/A'} ／ S&P 500：{fmt_quote(gspc) if gspc else 'N/A'}
• 新聞筆數：{news_quality}
{vip_block}

【本時段美股新聞】
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
    return claude_call(prompt, max_tokens=2500)


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

    def _fmt_crypto(d: dict) -> str:
        if not d:
            return "N/A"
        return f"{d.get('emoji','')} {d.get('price','N/A')} ({d.get('pct','N/A')})"

    prompt = f"""你是一位國際財經分析師。請根據以下市場數據與新聞，以繁體中文撰寫國際財經分析。{weekend_note}
時間：{now_str()}，時段：「{session_info['label']}」（{session_info['period']}）

【注意】：
- 直接開始寫分析內容，不要輸出 PHASE / 階段 / 資訊收斂 等標籤
- 禁空話，禁 Markdown 表格，全程 bullet（•）
- 台灣慣例：🔴=漲/利多，🟢=跌/利空
- 每個論點格式：「事件/數據（數值）→ 傳導機制 → 影響方向」

【市場數據】
• VIX：{fmt_quote(vix) if vix else 'N/A'} ／ 美債10Y：{fmt_quote(us10y) if us10y else 'N/A'} ／ DXY：{fmt_quote(dxy) if dxy else 'N/A'}
• 黃金：{fmt_quote(gold) if gold else 'N/A'} ／ 原油(WTI)：{fmt_quote(oil) if oil else 'N/A'}
• BTC：{_fmt_crypto(btc)} ／ ETH：{_fmt_crypto(eth)}
{'• 金十數據：快訊已附入（見下方）' if jin10_text else '• 金十數據：未取得'}

【本時段國際新聞】
{build_news_text(news)}
{jin10_text if jin10_text else ''}

請完整輸出以下五節（900字以內，篇幅自由調配）：

**A. 全球歸因分析**
• VIX/美債/DXY 說明什麼？Risk-On 還是 Risk-Off？
• 1-3 個最主要驅動事件 → 傳導路徑

**B. 重大事件影響**
• 事件（數值）→ 傳導路徑 → 🔴利多/🟢利空

**C. 大宗商品與加密**
• 品項：漲跌 → 驅動因素 → 對通膨/央行政策的隱含訊號

**D. 多空決策**
• 多方力量（1-2個）→ 受惠資產/族群
• 空方風險（1-2個）→ 受壓資產/族群
• 對台股的具體傳導路徑：哪些族群（代號）受惠或受壓

**E. 本週重要財經事件**（若有）
• 數據名稱：預期值 → 若超預期/不如預期，影響方向

> ⚠️ 以上為 AI 生成分析，不構成投資建議。"""
    return claude_call(prompt, max_tokens=2500)


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
    return claude_call(prompt, max_tokens=1500)


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
    return claude_call(prompt, max_tokens=2000)


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
