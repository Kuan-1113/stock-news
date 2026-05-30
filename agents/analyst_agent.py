"""
agents/analyst_agent.py — Claude AI 分析 Agent
台股 / 美股 / 全球 三份分析並行執行
原本順序執行 ~3 分鐘 → 並行後 ~1 分鐘
"""

import requests
from concurrent.futures import ThreadPoolExecutor

from shared.config import ANTHROPIC_API_KEY
from shared.utils import (
    build_news_text, fmt_quote, now_str, is_weekend
)


# ── Claude API 呼叫 ───────────────────────────────────────────────

def claude_call(prompt: str, max_tokens: int = 1800) -> str:
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
                "model": "claude-opus-4-5",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if r.status_code == 200:
            return r.json()["content"][0]["text"].strip()
        else:
            print(f"❌ Claude API 錯誤：HTTP {r.status_code} {r.text[:200]}")
            return f"AI 分析暫時無法使用（HTTP {r.status_code}）"
    except Exception as e:
        print(f"❌ Claude 呼叫失敗：{e}")
        return f"AI 分析暫時無法使用（{str(e)[:100]}）"


# ── 各市場分析 Prompt ─────────────────────────────────────────────

def analyze_tw(news: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(news)
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

def analyze_us(news: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(news)
    dji  = market_data.get("dji",  {})
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

def analyze_global(news: list, session_info: dict, market_data: dict) -> str:
    news_text = build_news_text(news)
    vix   = market_data.get("vix",   {})
    gold  = market_data.get("gold",  {})
    oil   = market_data.get("oil",   {})
    dxy   = market_data.get("dxy",   {})
    us10y = market_data.get("us10y", {})
    crypto = market_data.get("crypto", {})
    btc   = crypto.get("BTC", {})
    eth   = crypto.get("ETH", {})
    weekend_note = "\n⚠️ 今日為週末，部分指標數據為上次交易日數值，已標註「未更新」。" if is_weekend() else ""

    def _fmt_crypto(d: dict) -> str:
        if not d:
            return "N/A"
        return f"{d.get('emoji','')} {d.get('price','N/A')} ({d.get('pct','N/A')})"

    prompt = f"""你是一位資深國際財經分析師。現在是台灣時間 {now_str()}，本次為「{session_info['label']}」（{session_info['period']}）。{weekend_note}

【全球關鍵指標】
- VIX 恐慌指數：{fmt_quote(vix) if vix else 'N/A'}
- 美債 10Y 殖利率：{fmt_quote(us10y) if us10y else 'N/A'}
- 美元指數 DXY：{fmt_quote(dxy) if dxy else 'N/A'}
- 黃金：{fmt_quote(gold) if gold else 'N/A'}
- 原油（WTI）：{fmt_quote(oil) if oil else 'N/A'}
- BTC：{_fmt_crypto(btc)}
- ETH：{_fmt_crypto(eth)}

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


# ── Agent ─────────────────────────────────────────────────────────

class AnalystAgent:
    """
    並行執行台股 / 美股 / 全球三份 Claude 分析
    回傳 {"tw": "...", "us": "...", "global": "..."}
    """

    def run(self, market_data: dict, news_data: dict, session_info: dict) -> dict:
        print("🤖 [AnalystAgent] 並行分析中（台股 / 美股 / 全球）...")

        with ThreadPoolExecutor(max_workers=3) as executor:
            future_tw     = executor.submit(analyze_tw,     news_data.get("tw", []),     session_info, market_data)
            future_us     = executor.submit(analyze_us,     news_data.get("us", []),     session_info, market_data)
            future_global = executor.submit(analyze_global, news_data.get("global", []), session_info, market_data)

            tw_result     = future_tw.result()
            us_result     = future_us.result()
            global_result = future_global.result()

        print("🤖 [AnalystAgent] 完成")
        return {"tw": tw_result, "us": us_result, "global": global_result}
