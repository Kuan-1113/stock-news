"""
agents/macro_agent.py — 總體經濟 Agent (B2)

資料來源：
  - FOMC 行事曆：2026 年硬編碼（聯準會官網已公告）
  - FRED API：美國 CPI / 失業率 / 聯邦基金利率（需設定 FRED_API_KEY）
  - 台灣出口數據：財政部統計資料（免費 API）

功能：
  - 計算距下次 FOMC 還有幾天，標記「本週 FOMC」
  - 取最近 FRED 總體指標（有 key 才啟用）
  - 取台灣最新出口年增率（台股科技股重要先行指標）
  - 整合成一段簡要總體環境快照，發送至 DISCORD_GLOBAL（08:00 早報）

環境變數（Railway）：
  FRED_API_KEY   — 選填，可從 https://fred.stlouisfed.org/docs/api/api_key.html 免費申請

整合位置：
  orchestrator.py → run_report() Phase 3.5 與 SentimentAgent 同層
"""

from __future__ import annotations

import os
import time
import datetime
import requests

from shared.config import ANTHROPIC_API_KEY, DISCORD_GLOBAL
from shared.utils  import send_discord_message, send_embed, now_str, is_weekend
from agents.analyst_agent import claude_call


# ── FOMC 行事曆（2026 年，聯準會已公告）────────────────────────────
# 來源：https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# 2026 年 8 場 FOMC 會議日期（最後一天）
_FOMC_DATES_2026: list[datetime.date] = [
    datetime.date(2026, 1, 29),
    datetime.date(2026, 3, 19),
    datetime.date(2026, 4, 30),
    datetime.date(2026, 6, 18),
    datetime.date(2026, 7, 30),
    datetime.date(2026, 9, 17),
    datetime.date(2026, 11, 5),
    datetime.date(2026, 12, 17),
]

# 跨年備用（2025 年若跨年用）
_FOMC_DATES_2025: list[datetime.date] = [
    datetime.date(2025, 12, 11),
]

_ALL_FOMC: list[datetime.date] = sorted(_FOMC_DATES_2025 + _FOMC_DATES_2026)


# ── FOMC 倒數 ──────────────────────────────────────────────────────

def get_fomc_status() -> dict:
    """
    計算距下次 FOMC 的天數。
    回傳：{
        "next_date":  date,
        "days_left":  int,         # 0 = 今日, 負數 = 剛過
        "this_week":  bool,        # 7日內
        "label":      str,         # 人類可讀說明
    }
    """
    today = datetime.date.today()
    # 找下一場（或今日）
    upcoming = [d for d in _ALL_FOMC if d >= today]
    if not upcoming:
        return {
            "next_date": None,
            "days_left": 999,
            "this_week": False,
            "label":     "（2026 FOMC 行事曆已結束，請更新）",
        }

    next_date = upcoming[0]
    days_left = (next_date - today).days
    this_week = days_left <= 7

    if days_left == 0:
        label = f"🚨 **今日 FOMC！** ({next_date.strftime('%m/%d')})"
    elif days_left <= 3:
        label = f"🔴 FOMC 即將來臨（{days_left} 天後，{next_date.strftime('%m/%d')}）"
    elif this_week:
        label = f"🟡 本週 FOMC（{days_left} 天後，{next_date.strftime('%m/%d')}）"
    else:
        label = f"🟢 下次 FOMC 還有 {days_left} 天（{next_date.strftime('%Y/%m/%d')}）"

    return {
        "next_date": next_date,
        "days_left": days_left,
        "this_week": this_week,
        "label":     label,
    }


# ── FRED API ──────────────────────────────────────────────────────

_FRED_BASE = "https://api.stlouisfed.org/fred"

def _fred_series(series_id: str, api_key: str, limit: int = 1) -> dict | None:
    """取 FRED 單一系列最新值"""
    try:
        r = requests.get(
            f"{_FRED_BASE}/series/observations",
            params={
                "series_id":      series_id,
                "api_key":        api_key,
                "file_type":      "json",
                "sort_order":     "desc",
                "limit":          limit,
                "observation_start": "2024-01-01",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None
        obs = r.json().get("observations", [])
        if not obs:
            return None
        # 找最新有值的觀測（排除 "."）
        for o in obs:
            if o.get("value", ".") != ".":
                return {"date": o["date"], "value": float(o["value"])}
        return None
    except Exception as e:
        print(f"  ⚠️ FRED {series_id} 失敗：{e}")
        return None


def fetch_fred_data() -> dict:
    """
    取 FRED 三大指標：
      - CPIAUCSL  ：美國 CPI 年增率（YoY %）
      - UNRATE    ：美國失業率（%）
      - FEDFUNDS  ：聯邦基金利率（%）
    需設定環境變數 FRED_API_KEY，否則跳過回傳空 dict。
    """
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        return {}

    print("  📡 FRED API 抓取中...")
    result: dict = {}

    # CPI（年增率 = 比去年同期）
    cpi_now  = _fred_series("CPIAUCSL", api_key, limit=1)
    cpi_year = _fred_series("CPIAUCSL", api_key, limit=14)  # ~13個月前
    if cpi_now and cpi_year:
        # 取 12個月前的值算 YoY
        obs12 = None
        try:
            r = requests.get(
                f"{_FRED_BASE}/series/observations",
                params={
                    "series_id":  "CPIAUCSL",
                    "api_key":    api_key,
                    "file_type":  "json",
                    "sort_order": "desc",
                    "limit":      14,
                },
                timeout=15,
            )
            all_obs = [o for o in r.json().get("observations", []) if o["value"] != "."]
            if len(all_obs) >= 13:
                obs12 = float(all_obs[12]["value"])
        except Exception:
            pass

        if obs12:
            yoy = (float(cpi_now["value"]) - obs12) / obs12 * 100
            result["cpi_yoy"] = {"value": round(yoy, 2), "date": cpi_now["date"]}
        else:
            result["cpi"] = cpi_now

    # 失業率
    unrate = _fred_series("UNRATE", api_key)
    if unrate:
        result["unrate"] = unrate

    # 聯邦基金利率
    fedfunds = _fred_series("FEDFUNDS", api_key)
    if fedfunds:
        result["fedfunds"] = fedfunds

    return result


# ── 台灣出口數據 ──────────────────────────────────────────────────

def fetch_taiwan_exports() -> dict:
    """
    財政部統計處 — 台灣出口年增率。
    免費公開 API，無需 Key。

    回傳：{"date": "2026-04", "yoy": 12.5, "label": "+12.5%（2026-04）"}
    """
    try:
        # 財政部統計資料服務網 API
        r = requests.get(
            "https://api.mof.gov.tw/mof/v1/ExportStats",
            params={
                "level":  "TT",     # 全體
                "limit":  1,
                "sort":   "Date desc",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if data and "data" in data and data["data"]:
                row = data["data"][0]
                date = row.get("Date", "")
                yoy  = float(row.get("ExportYoY", 0) or 0)
                return {
                    "date":  date,
                    "yoy":   round(yoy, 2),
                    "label": f"{yoy:+.1f}%（{date}）",
                }
    except Exception:
        pass

    # Fallback：用財政部另一個公開資料集
    try:
        # DGBAS 主計處 API 或直接取 Yahoo 台灣出口 ETF 作為 proxy
        r2 = requests.get(
            "https://www.mof.gov.tw/multiplehtml/384fb3077bb349ea973e7fc6f13b6974",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        # 無法直接解析，降級失敗
        return {}
    except Exception:
        return {}


# ── Claude 簡評 ───────────────────────────────────────────────────

def _build_macro_prompt(
    fomc:    dict,
    fred:    dict,
    exports: dict,
) -> str:
    lines = []

    # FOMC
    lines.append(f"聯準會 FOMC：{fomc.get('label', 'N/A')}")

    # FRED
    if fred:
        cpi = fred.get("cpi_yoy", fred.get("cpi", {}))
        if cpi:
            lines.append(f"美國 CPI 年增率：{cpi.get('value', 'N/A')}%（{cpi.get('date', '')}）")
        unrate = fred.get("unrate")
        if unrate:
            lines.append(f"美國失業率：{unrate['value']}%（{unrate['date']}）")
        ff = fred.get("fedfunds")
        if ff:
            lines.append(f"聯邦基金利率：{ff['value']}%（{ff['date']}）")
    else:
        lines.append("美國總體指標：（FRED_API_KEY 未設定，跳過）")

    # 台灣出口
    if exports:
        lines.append(f"台灣出口年增率：{exports.get('label', 'N/A')}")
    else:
        lines.append("台灣出口數據：（暫無法取得）")

    data_block = "\n".join(lines)

    return f"""你是總體經濟分析師，負責每日 08:00 早報的總體環境掃描。
以下是今日重要總體數據：

{data_block}

請用繁體中文撰寫今日總體環境簡評（150 字以內，bullet 格式）：

• FOMC 會議影響評估（距今幾天？對台股/美股的潛在影響）
• 通膨與就業環境（偏鬆 / 偏緊 / 中性）
• 台灣出口訊號（對台股科技股的影響）
• 一句話總體環境定調（偏多 / 中性 / 偏空 / 謹慎）

> ⚠️ AI 生成，不構成投資建議。"""


# ── Agent 主類別 ──────────────────────────────────────────────────

class MacroAgent:
    """
    總體經濟 Agent。
    run() 自動抓資料 + Claude 簡評 + 發送到 DISCORD_GLOBAL。
    每次報告都執行（08:00 / 14:00 / 22:00 均會呼叫）。
    """

    def run(self) -> dict:
        """回傳 {fomc, fred, exports, analysis}"""
        print("🌐 [MacroAgent] 抓取總體數據...")

        fomc    = get_fomc_status()
        fred    = fetch_fred_data()
        exports = fetch_taiwan_exports()

        print(f"  📅 {fomc['label']}")
        if fred:
            print(f"  🇺🇸 FRED：CPI={fred.get('cpi_yoy',{}).get('value','N/A')}%  "
                  f"UNRATE={fred.get('unrate',{}).get('value','N/A')}%  "
                  f"FF={fred.get('fedfunds',{}).get('value','N/A')}%")
        if exports:
            print(f"  🇹🇼 台灣出口：{exports.get('label','N/A')}")

        # Claude 分析
        analysis = ""
        if ANTHROPIC_API_KEY:
            try:
                prompt   = _build_macro_prompt(fomc, fred, exports)
                analysis = claude_call(prompt, max_tokens=400)
                print("  ✅ Claude 總體簡評完成")
            except Exception as e:
                print(f"  ⚠️ Claude 分析失敗：{e}")
                analysis = "（AI 分析暫時無法生成）"

        self._send(fomc, fred, exports, analysis)

        return {
            "fomc":     fomc,
            "fred":     fred,
            "exports":  exports,
            "analysis": analysis,
        }

    # ── Discord 發送 ──────────────────────────────────────────────

    def _send(
        self,
        fomc:     dict,
        fred:     dict,
        exports:  dict,
        analysis: str,
    ) -> None:
        if not DISCORD_GLOBAL:
            print("  ⚠️ DISCORD_GLOBAL 未設定，跳過發送")
            return

        ts             = now_str()
        weekend_banner = "（週末版）" if is_weekend() else ""

        # FOMC 欄位
        fomc_text = fomc.get("label", "N/A")
        if fomc.get("this_week"):
            fomc_text += "\n⚠️ 本週有 FOMC，盤面波動風險加大"

        # FRED 欄位
        if fred:
            cpi    = fred.get("cpi_yoy", fred.get("cpi", {}))
            unrate = fred.get("unrate", {})
            fedfunds = fred.get("fedfunds", {})
            fred_text = "\n".join(filter(None, [
                f"CPI 年增率：`{cpi.get('value','N/A')}%`（{cpi.get('date','')}）" if cpi else None,
                f"失業率：`{unrate.get('value','N/A')}%`（{unrate.get('date','')}）"   if unrate else None,
                f"聯邦基金利率：`{fedfunds.get('value','N/A')}%`（{fedfunds.get('date','')}）" if fedfunds else None,
            ]))
        else:
            fred_text = "FRED_API_KEY 未設定\n免費申請：fred.stlouisfed.org"

        # 台灣出口
        exports_text = exports.get("label", "暫無資料") if exports else "暫無資料"

        send_embed(DISCORD_GLOBAL, {
            "title":       f"🌐 總體經濟快照 {weekend_banner}| {ts}",
            "description": "FOMC 行事曆 + 美國總體指標 + 台灣出口",
            "color":       0x2C3E50,   # 深灰藍
            "fields": [
                {"name": "🏦 聯準會 FOMC",   "value": fomc_text,     "inline": False},
                {"name": "🇺🇸 美國總體指標", "value": fred_text,     "inline": True},
                {"name": "🇹🇼 台灣出口",     "value": exports_text,  "inline": True},
            ],
            "footer": {
                "text": (
                    "來源：FED FOMC 行事曆"
                    + (" / FRED API" if fred else " / FRED（未設定）")
                    + " / 財政部統計處"
                )
            },
        })
        time.sleep(1.2)

        if analysis and "跳過" not in analysis and "失敗" not in analysis:
            send_discord_message(
                DISCORD_GLOBAL,
                f"## 🤖 Claude AI — 總體環境簡評 | {ts}\n\n{analysis}"
            )

        print("  ✅ [MacroAgent] Discord 發送完成")
