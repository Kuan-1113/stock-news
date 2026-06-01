"""
agents/crypto_agent.py — 加密貨幣市場輕量整合 Agent (A1)

資料來源（全部免費，無需 API Key）：
  - BTC / ETH / SOL 價格 + 24h 漲跌：CoinGecko public API
  - 恐懼貪婪指數：alternative.me/crypto/fear-and-greed-index/
  - BTC 資金費率（永續合約）：Binance public API

功能：
  - 每日 22:00 盤後晚報，附加到 DISCORD_GLOBAL 國際頻道
  - Claude 一段簡評（加密市場 + 對台股/美股的相關性提示）

不依賴：
  - 不需 Binance API Key（使用 Binance 公開 endpoint）
  - 不需 CoinGecko Pro（使用免費版）
  - 與獨立的 crypto-agent 專案無關（那是合約交易信號系統）
"""

from __future__ import annotations

import time
import requests

from shared.config  import ANTHROPIC_API_KEY, DISCORD_GLOBAL
from shared.utils   import send_discord_message, send_embed, now_str, is_weekend
from agents.analyst_agent import claude_call


# ── 資料抓取 ──────────────────────────────────────────────────────

def _fetch_coingecko() -> dict:
    """
    BTC / ETH / SOL 價格、24h 漲跌幅、7d 漲跌幅。
    回傳範例：
      {"BTC": {"price": 68500.0, "pct_24h": +1.2, "pct_7d": -3.4, "emoji": "🟢"}}
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids":                "bitcoin,ethereum,solana",
                "vs_currencies":      "usd",
                "include_24hr_change": "true",
                "include_7d_change":   "true",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        data    = r.json()
        mapping = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}
        result  = {}
        for sym, cg_id in mapping.items():
            if cg_id not in data:
                continue
            price  = data[cg_id]["usd"]
            p24    = data[cg_id].get("usd_24h_change") or 0.0
            p7     = data[cg_id].get("usd_7d_change")  or 0.0
            result[sym] = {
                "price":   price,
                "pct_24h": round(p24, 2),
                "pct_7d":  round(p7,  2),
                "emoji":   "🔴" if p24 < 0 else "🟢",
            }
        return result
    except Exception as e:
        print(f"  ⚠️ CoinGecko 失敗：{e}")
        return {}


def _fetch_fear_greed() -> dict:
    """
    Alternative.me 恐懼貪婪指數（免費公開）。
    回傳：{"value": 55, "label": "Greed", "emoji": "😊"}
    """
    try:
        r = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        d     = r.json()["data"][0]
        value = int(d["value"])
        label = d["value_classification"]  # "Extreme Fear" / "Fear" / "Neutral" / "Greed" / "Extreme Greed"
        emoji_map = {
            "Extreme Fear":  "😱",
            "Fear":          "😨",
            "Neutral":       "😐",
            "Greed":         "😊",
            "Extreme Greed": "🤑",
        }
        return {
            "value": value,
            "label": label,
            "emoji": emoji_map.get(label, "❓"),
        }
    except Exception as e:
        print(f"  ⚠️ Fear & Greed 失敗：{e}")
        return {}


def _fetch_binance_funding(symbol: str = "BTCUSDT") -> dict:
    """
    Binance 永續合約最新資金費率（免費公開，無需 API Key）。
    回傳：{"rate": 0.0001, "pct_str": "+0.0100%", "sentiment": "多頭擁擠"}
    正資金費率 = 多頭付費給空頭 = 市場偏多
    負資金費率 = 空頭付費給多頭 = 市場偏空
    """
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        rate = float(r.json().get("lastFundingRate", 0))
        pct  = rate * 100
        if pct > 0.05:
            sentiment = "多頭擁擠（偏高）"
        elif pct < -0.01:
            sentiment = "空頭情緒（偏負）"
        else:
            sentiment = "情緒中性"
        return {
            "rate":      rate,
            "pct_str":   f"{pct:+.4f}%",
            "sentiment": sentiment,
        }
    except Exception as e:
        print(f"  ⚠️ Binance 資金費率失敗：{e}")
        return {}


# ── Claude 簡評 ───────────────────────────────────────────────────

def _build_crypto_prompt(
    prices:     dict,
    fear_greed: dict,
    funding:    dict,
) -> str:
    """組合 Claude prompt 字串"""
    lines: list[str] = []

    # 價格行情
    if prices:
        for sym, d in prices.items():
            lines.append(
                f"{sym}：${d['price']:,.2f}  "
                f"24h {d['pct_24h']:+.2f}%  "
                f"7d {d['pct_7d']:+.2f}%"
            )
    else:
        lines.append("（行情資料暫時無法取得）")

    # 恐懼貪婪
    if fear_greed:
        lines.append(
            f"\n恐懼貪婪指數：{fear_greed['emoji']} {fear_greed['value']} "
            f"— {fear_greed['label']}"
        )
    else:
        lines.append("\n恐懼貪婪指數：（無法取得）")

    # 資金費率
    if funding:
        lines.append(
            f"BTC 資金費率：{funding['pct_str']}  {funding['sentiment']}"
        )
    else:
        lines.append("BTC 資金費率：（無法取得）")

    data_block = "\n".join(lines)

    prompt = f"""你是一位兼顧加密市場與台股/美股的跨市場分析師。
以下是今日加密貨幣市場數據：

{data_block}

請用繁體中文撰寫一段簡短評論（200 字以內，bullet 格式，禁 Markdown 表格）：

• 今日加密市場總結（BTC 方向 + 情緒）
• 資金費率解讀（多空偏向，是否過熱？）
• 對台股/美股的參考意義（流動性或風險偏好角度）
• 一句話操作提醒（謹慎 / 觀望 / 偏多 / 偏空）

> ⚠️ AI 生成，不構成投資建議。"""

    return prompt


# ── Agent 主類別 ──────────────────────────────────────────────────

class CryptoAgent:
    """
    加密貨幣輕量 Agent。
    呼叫 run() 後自動抓資料 + 請 Claude 簡評 + 發送到 DISCORD_GLOBAL。
    僅在 22:00 盤後晚報執行（由 orchestrator 決定時機）。
    """

    def run(self) -> dict:
        """
        執行完整流程，回傳資料 dict（供 orchestrator 整合日誌用）。
        dict keys: prices, fear_greed, funding, analysis
        """
        print("🪙 [CryptoAgent] 抓取加密貨幣資料...")

        prices     = _fetch_coingecko()
        fear_greed = _fetch_fear_greed()
        funding    = _fetch_binance_funding("BTCUSDT")

        # 格式化 debug log
        if prices:
            for sym, d in prices.items():
                print(f"  {d['emoji']} {sym}：${d['price']:,.2f} ({d['pct_24h']:+.2f}%)")
        if fear_greed:
            print(f"  {fear_greed['emoji']} F&G：{fear_greed['value']} ({fear_greed['label']})")
        if funding:
            print(f"  📐 BTC 資金費率：{funding['pct_str']} — {funding['sentiment']}")

        # Claude 簡評
        analysis = ""
        if ANTHROPIC_API_KEY:
            try:
                prompt   = _build_crypto_prompt(prices, fear_greed, funding)
                analysis = claude_call(prompt, max_tokens=500)
                print("  ✅ Claude 加密簡評完成")
            except Exception as e:
                print(f"  ⚠️ Claude 簡評失敗：{e}")
                analysis = "（AI 簡評暫時無法生成）"
        else:
            analysis = "（未設定 ANTHROPIC_API_KEY，跳過 AI 分析）"

        # 發送 Discord
        self._send(prices, fear_greed, funding, analysis)

        return {
            "prices":     prices,
            "fear_greed": fear_greed,
            "funding":    funding,
            "analysis":   analysis,
        }

    # ── Discord 發送 ──────────────────────────────────────────────

    def _send(
        self,
        prices:     dict,
        fear_greed: dict,
        funding:    dict,
        analysis:   str,
    ) -> None:
        if not DISCORD_GLOBAL:
            print("  ⚠️ DISCORD_GLOBAL 未設定，跳過發送")
            return

        ts             = now_str()
        weekend_banner = "（週末版）" if is_weekend() else ""

        # ── Embed：行情 + 情緒指標 ────────────────────────────────
        price_lines = "\n".join([
            f"{d['emoji']} **{sym}**：`${d['price']:>12,.2f}`  "
            f"24h {d['pct_24h']:+.2f}%  7d {d['pct_7d']:+.2f}%"
            for sym, d in prices.items()
        ]) if prices else "暫無資料"

        fg_text  = (
            f"{fear_greed['emoji']} **{fear_greed['value']}** — {fear_greed['label']}"
            if fear_greed else "暫無資料"
        )
        fund_text = (
            f"`{funding['pct_str']}`  {funding['sentiment']}"
            if funding else "暫無資料"
        )

        send_embed(DISCORD_GLOBAL, {
            "title":       f"🪙 加密貨幣市場快照 {weekend_banner}| {ts}",
            "description": "BTC / ETH / SOL 行情 + 市場情緒（僅供參考，非投資建議）",
            "color":       0xF39C12,   # 橙色（與自選股橙色相近，但用在國際頻道）
            "fields": [
                {"name": "💰 主流幣行情",   "value": price_lines, "inline": False},
                {"name": "😱 恐懼貪婪指數", "value": fg_text,     "inline": True},
                {"name": "📐 BTC 資金費率", "value": fund_text,   "inline": True},
            ],
            "footer": {"text": "資料來源：CoinGecko / Alternative.me / Binance Public API"},
        })
        time.sleep(1.2)

        # ── Claude 簡評 ───────────────────────────────────────────
        if analysis and "跳過" not in analysis and "失敗" not in analysis:
            send_discord_message(
                DISCORD_GLOBAL,
                f"## 🤖 Claude AI — 加密市場簡評 | {ts}\n\n{analysis}"
            )

        print("  ✅ [CryptoAgent] Discord 發送完成")
