"""
agents/sentiment_agent.py — 市場情緒 Agent (B1)

資料來源（全部免費，無需 API Key）：
  - 台指期三大法人淨部位：TAIFEX OpenAPI
  - Put/Call Ratio：TAIFEX OpenAPI
  - VIX：market_data dict（MarketDataAgent 已抓，直接傳入）

輸出：
  - 合成情緒分數（0~100，50 為中性）
  - 情緒標籤（極度恐懼 / 恐懼 / 中性 / 貪婪 / 極度貪婪）
  - 短段分析文字（供 AnalystAgent 加入 Claude prompt）
  - Discord Embed 發送至 DISCORD_GLOBAL（08:00 早報）

整合位置：
  orchestrator.py → _run_morning_sentiment(market_data)
  於 Phase 3 發送完大盤後呼叫（不阻塞主流程，有 try/except 保護）
"""

from __future__ import annotations

import time
import datetime
import requests

from shared.config import ANTHROPIC_API_KEY, DISCORD_GLOBAL
from shared.utils  import send_discord_message, send_embed, now_str, is_weekend
from agents.analyst_agent import claude_call


# ── TAIFEX 資料抓取 ───────────────────────────────────────────────

_TAIFEX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept":     "application/json",
}
_TIMEOUT = 15


def _fetch_taifex_institutional() -> dict:
    """
    TAIFEX OpenAPI — 三大法人台指期未平倉淨部位（口數）。

    外資多空 + 投信多空 + 自營商多空 → 三大法人合計淨口數
    正數 = 淨多 = 偏多情緒；負數 = 淨空 = 偏空情緒

    API：/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate
    ContractCode = "臺股期貨"；欄位 OpenInterest(Net) 即淨口數（正=淨多）

    回傳範例：
      {
        "date":        "20260529",
        "foreign_net": -60650,   # 外資台指期淨口數（正=淨多）
        "trust_net":   46926,    # 投信淨口數
        "dealer_net":  2074,     # 自營商淨口數
        "total_net":   -11650,   # 三大合計
      }
    回傳空 dict 代表 API 失敗。
    """
    try:
        r = requests.get(
            "https://openapi.taifex.com.tw/v1"
            "/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
            headers=_TAIFEX_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"  ⚠️ TAIFEX 三大法人 HTTP {r.status_code}")
            return {}

        data = r.json()
        if not data:
            return {}

        # 過濾台指期（ContractCode 含「臺股」）
        tx_rows = [row for row in data if "臺股" in row.get("ContractCode", "")]
        if not tx_rows:
            tx_rows = data  # fallback 用全部

        # 取最新一天
        tx_rows.sort(key=lambda x: x.get("Date", ""), reverse=True)
        latest_date = tx_rows[0].get("Date", "")
        today_rows  = [row for row in tx_rows if row.get("Date", "") == latest_date]

        _int = lambda x: int(str(x).replace(",", "") or 0)

        foreign_net = trust_net = dealer_net = 0
        for row in today_rows:
            item = row.get("Item", "")
            net  = _int(row.get("OpenInterest(Net)", 0))
            if "外資" in item or "foreign" in item.lower():
                foreign_net = net
            elif "投信" in item or "trust" in item.lower():
                trust_net = net
            elif "自營" in item or "dealer" in item.lower():
                dealer_net = net

        return {
            "date":        latest_date,
            "foreign_net": foreign_net,
            "trust_net":   trust_net,
            "dealer_net":  dealer_net,
            "total_net":   foreign_net + trust_net + dealer_net,
        }

    except Exception as e:
        print(f"  ⚠️ TAIFEX 三大法人失敗：{e}")
        return {}


def _fetch_put_call_ratio() -> dict:
    """
    TAIFEX OpenAPI — Put/Call Ratio（台指選擇權）。

    PCR < 0.7  → 市場偏樂觀（貪婪）
    PCR 0.7~1.0 → 中性
    PCR > 1.0  → 市場偏悲觀（恐懼）

    回傳範例：{"date": "2026-05-31", "pcr": 0.85, "label": "中性偏謹慎"}
    """
    try:
        r = requests.get(
            "https://openapi.taifex.com.tw/v1/PutCallRatio",
            headers=_TAIFEX_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"  ⚠️ TAIFEX PCR HTTP {r.status_code}")
            return {}

        data = r.json()
        if not data:
            return {}

        # 取最新一筆
        data.sort(key=lambda x: x.get("Date", ""), reverse=True)
        latest = data[0]

        _float = lambda x: float(str(x).replace(",", "") or 0)

        # 欄位名稱：PutCallVolumeRatio%（例如 "124.82"，代表 PCR = 1.2482）
        # 備用欄位：PutCallOIRatio%（用未平倉量計算）
        raw = (
            latest.get("PutCallVolumeRatio%")
            or latest.get("PutCallOIRatio%")
            or latest.get("PutCallVolumeRatio")
            or latest.get("PCRatio")
            or 0
        )
        pcr = _float(raw)
        # 欄位已是百分比形式（124.82 = 1.2482），除以 100 轉回比值
        if pcr > 5:
            pcr /= 100

        if pcr < 0.7:
            label = "偏樂觀（多頭）"
        elif pcr < 1.0:
            label = "中性偏謹慎"
        else:
            label = "偏悲觀（空頭）"

        return {
            "date":  latest.get("Date", ""),
            "pcr":   round(pcr, 4),
            "label": label,
        }

    except Exception as e:
        print(f"  ⚠️ TAIFEX PCR 失敗：{e}")
        return {}


# ── 合成情緒分數 ──────────────────────────────────────────────────

def calc_sentiment_score(
    institutional: dict,
    pcr:           dict,
    vix_val:       float | None,
) -> dict:
    """
    0~100 情緒分數（50 = 中性，>50 = 偏多，<50 = 偏空）。

    計分規則（各項 0~100，加權平均）：
      - 三大法人台指期淨口數（40%）
          > +5000  → 80    0~+5000 → 50~80    < 0 → 20~50
      - Put/Call Ratio（30%）
          < 0.7 → 75    0.7~1.0 → 50~75    > 1.0 → 25~50
      - VIX（30%）
          < 15 → 80    15~20 → 60~80    20~30 → 30~60    > 30 → 20
    """
    scores: list[tuple[float, float]] = []   # (score, weight)

    # 1. 三大法人淨口數
    if institutional and "total_net" in institutional:
        net = institutional["total_net"]
        if net >= 5000:
            s = 80.0
        elif net >= 0:
            s = 50.0 + (net / 5000) * 30
        elif net >= -5000:
            s = 50.0 + (net / 5000) * 30   # net < 0 → s < 50
        else:
            s = 20.0
        scores.append((round(s, 1), 0.40))

    # 2. PCR
    if pcr and "pcr" in pcr:
        p = pcr["pcr"]
        if p < 0.7:
            s = 75.0
        elif p < 1.0:
            s = 75.0 - ((p - 0.7) / 0.3) * 25
        else:
            s = max(25.0, 50.0 - (p - 1.0) * 50)
        scores.append((round(s, 1), 0.30))

    # 3. VIX
    if vix_val is not None and vix_val > 0:
        v = vix_val
        if v < 15:
            s = 80.0
        elif v < 20:
            s = 80.0 - ((v - 15) / 5) * 20
        elif v < 30:
            s = 60.0 - ((v - 20) / 10) * 30
        else:
            s = 20.0
        scores.append((round(s, 1), 0.30))

    if not scores:
        return {"score": 50.0, "label": "資料不足", "emoji": "❓"}

    # 加權平均（重新歸一化，避免部分資料缺失時分母不足 1）
    total_w = sum(w for _, w in scores)
    score   = sum(s * w for s, w in scores) / total_w

    # 標籤
    if score >= 75:
        label, emoji = "極度貪婪", "🤑"
    elif score >= 60:
        label, emoji = "偏多 / 貪婪", "😊"
    elif score >= 40:
        label, emoji = "中性", "😐"
    elif score >= 25:
        label, emoji = "偏空 / 恐懼", "😨"
    else:
        label, emoji = "極度恐懼", "😱"

    return {
        "score":  round(score, 1),
        "label":  label,
        "emoji":  emoji,
        "detail": {s_item: round(s, 1) for s_item, (s, _) in zip(
            ["institutional", "pcr", "vix"], scores
        )},
    }


# ── Claude 分析 ──────────────────────────────────────────────────

def _build_sentiment_prompt(
    institutional: dict,
    pcr:           dict,
    vix_val:       float | None,
    sentiment:     dict,
) -> str:
    lines = []

    # 三大法人
    if institutional:
        lines.append(
            f"台指期三大法人淨口數（{institutional.get('date','')}）：\n"
            f"  外資 {institutional.get('foreign_net', 0):+,} 口  "
            f"投信 {institutional.get('trust_net', 0):+,} 口  "
            f"自營 {institutional.get('dealer_net', 0):+,} 口  "
            f"合計 {institutional.get('total_net', 0):+,} 口"
        )
    else:
        lines.append("台指期三大法人：（資料無法取得）")

    # PCR
    if pcr:
        lines.append(
            f"Put/Call Ratio（{pcr.get('date','')}）：{pcr.get('pcr','N/A')}  "
            f"→ {pcr.get('label','')}"
        )
    else:
        lines.append("Put/Call Ratio：（資料無法取得）")

    # VIX
    if vix_val:
        lines.append(f"VIX 恐慌指數：{vix_val:.2f}")
    else:
        lines.append("VIX：（資料無法取得）")

    # 合成分數
    lines.append(
        f"\n合成情緒分數：{sentiment['emoji']} {sentiment['score']}/100  —  {sentiment['label']}"
    )

    data_block = "\n".join(lines)

    return f"""你是一位專業台股衍生品分析師。以下是今日台股市場情緒數據：

{data_block}

請用繁體中文撰寫今日情緒摘要（150 字以內，bullet 格式，禁 Markdown 表格）：

• 三大法人籌碼解讀（外資方向最重要）
• PCR 與 VIX 綜合情緒判斷
• 今日盤面操作建議（1 句話，偏多 / 中性 / 偏空 / 謹慎）

> ⚠️ AI 生成，不構成投資建議。"""


# ── Agent 主類別 ──────────────────────────────────────────────────

class SentimentAgent:
    """
    市場情緒 Agent。
    run(market_data) 接收 MarketDataAgent 的輸出，提取 VIX，
    再自行抓 TAIFEX 資料，計算合成情緒分數，發送至 DISCORD_GLOBAL。
    """

    def run(self, market_data: dict | None = None) -> dict:
        """
        執行完整流程，回傳情緒 dict：
          {institutional, pcr, vix, sentiment, analysis}
        """
        print("📊 [SentimentAgent] 抓取市場情緒資料...")

        # 從 market_data 取 VIX（已由 MarketDataAgent 抓好）
        vix_val: float | None = None
        if market_data:
            vix_q = market_data.get("vix", {})
            try:
                # price 格式如 "18.45"
                raw = str(vix_q.get("price", "")).replace(",", "")
                vix_val = float(raw) if raw not in ("N/A", "", "None") else None
            except ValueError:
                vix_val = None

        # 抓 TAIFEX 資料
        institutional = _fetch_taifex_institutional()
        pcr           = _fetch_put_call_ratio()

        # Debug log
        if institutional:
            print(
                f"  📐 三大法人台指期：外資 {institutional.get('foreign_net',0):+,}  "
                f"投信 {institutional.get('trust_net',0):+,}  "
                f"自營 {institutional.get('dealer_net',0):+,}  "
                f"合計 {institutional.get('total_net',0):+,}"
            )
        else:
            print("  ⚠️ 三大法人資料取得失敗")

        if pcr:
            print(f"  📐 PCR：{pcr.get('pcr','N/A')} — {pcr.get('label','')}")
        else:
            print("  ⚠️ PCR 資料取得失敗")

        if vix_val:
            print(f"  📐 VIX：{vix_val:.2f}")

        # 合成情緒分數
        sentiment = calc_sentiment_score(institutional, pcr, vix_val)
        print(f"  {sentiment['emoji']} 情緒分數：{sentiment['score']}/100 — {sentiment['label']}")

        # Claude 分析
        analysis = ""
        if ANTHROPIC_API_KEY:
            try:
                prompt   = _build_sentiment_prompt(institutional, pcr, vix_val, sentiment)
                analysis = claude_call(prompt, max_tokens=400)
                print("  ✅ Claude 情緒分析完成")
            except Exception as e:
                print(f"  ⚠️ Claude 分析失敗：{e}")
                analysis = "（AI 分析暫時無法生成）"

        # 發送 Discord
        self._send(institutional, pcr, vix_val, sentiment, analysis)

        return {
            "institutional": institutional,
            "pcr":           pcr,
            "vix":           vix_val,
            "sentiment":     sentiment,
            "analysis":      analysis,
        }

    # ── Discord 發送 ──────────────────────────────────────────────

    def _send(
        self,
        institutional: dict,
        pcr:           dict,
        vix_val:       float | None,
        sentiment:     dict,
        analysis:      str,
    ) -> None:
        if not DISCORD_GLOBAL:
            print("  ⚠️ DISCORD_GLOBAL 未設定，跳過發送")
            return

        ts             = now_str()
        weekend_banner = "（週末版）" if is_weekend() else ""

        # 三大法人欄位文字
        if institutional:
            inst_text = (
                f"外資 `{institutional.get('foreign_net',0):+,}` 口\n"
                f"投信 `{institutional.get('trust_net',0):+,}` 口\n"
                f"自營 `{institutional.get('dealer_net',0):+,}` 口\n"
                f"**合計 `{institutional.get('total_net',0):+,}` 口**"
            )
        else:
            inst_text = "暫無資料（TAIFEX API）"

        pcr_text = (
            f"`{pcr.get('pcr','N/A')}`  —  {pcr.get('label','')}"
            if pcr else "暫無資料"
        )
        vix_text = f"`{vix_val:.2f}`" if vix_val else "暫無資料"

        score_bar = _score_bar(sentiment["score"])
        score_text = (
            f"{sentiment['emoji']} **{sentiment['score']}/100** — {sentiment['label']}\n"
            f"`{score_bar}`"
        )

        send_embed(DISCORD_GLOBAL, {
            "title":       f"📊 台股情緒儀錶板 {weekend_banner}| {ts}",
            "description": "三大法人期貨部位 + PCR + VIX 合成情緒分析",
            "color":       0x1ABC9C,   # 青綠色
            "fields": [
                {"name": "🏦 台指期三大法人淨口數", "value": inst_text,  "inline": True},
                {"name": "🎯 Put/Call Ratio",       "value": pcr_text,  "inline": True},
                {"name": "😱 VIX 恐慌指數",          "value": vix_text,  "inline": True},
                {"name": "🌡️ 合成情緒分數",          "value": score_text,"inline": False},
            ],
            "footer": {"text": "資料來源：TAIFEX OpenAPI / Yahoo Finance (VIX)"},
        })
        time.sleep(1.2)

        if analysis and "暫時" not in analysis and "未設定" not in analysis:
            send_discord_message(
                DISCORD_GLOBAL,
                f"## 🤖 Claude AI — 市場情緒解讀 | {ts}\n\n{analysis}"
            )

        print("  ✅ [SentimentAgent] Discord 發送完成")


def _score_bar(score: float, width: int = 20) -> str:
    """把 0~100 的分數畫成文字進度條，例如 '█████████░░░░░░░░░░░'"""
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)
