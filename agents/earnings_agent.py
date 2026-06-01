"""
agents/earnings_agent.py — 財報 / 法說會行事曆 Agent (B3)

資料來源（全部免費）：
  - 台股法說會：公開資訊觀測站 MOPS（POST 請求，HTML 解析）
  - 美股財報：Yahoo Finance earnings calendar（JSON endpoint）

功能：
  - 顯示「本週 + 下週」台股法說會清單（公司名稱、日期、地點）
  - 顯示「本週」重要美股財報（僅顯示台股關聯度高的科技大型股）
  - 08:00 早報發送至 DISCORD_GLOBAL

設計考量：
  - MOPS HTML 解析可能因改版失效，有完整 fallback（空資料直接跳過）
  - Yahoo Finance earnings 有公開 JSON API，相對穩定
  - 整個 Agent 有 try/except 外層保護，失效不影響主報告
"""

from __future__ import annotations

import time
import datetime
import requests
from html.parser import HTMLParser

from shared.config import DISCORD_GLOBAL
from shared.utils  import send_embed, now_str, is_weekend


# ── 台股法說會（MOPS）────────────────────────────────────────────

class _TableParser(HTMLParser):
    """輕量 HTML 表格解析器，從 MOPS 抓法說會資料"""

    def __init__(self) -> None:
        super().__init__()
        self.rows:      list[list[str]] = []
        self._row:      list[str]       = []
        self._cell:     str             = ""
        self._in_cell:  bool            = False
        self._in_table: bool            = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "table":
            self._in_table = True
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell    = ""

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._in_cell:
            self._row.append(self._cell.strip())
            self._in_cell = False
        elif tag == "tr" and self._in_table and self._row:
            self.rows.append(self._row)
            self._row = []
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell += data


def fetch_mops_investor_conf(weeks: int = 2) -> list[dict]:
    """
    抓取公開資訊觀測站近 2 週法說會資料。
    回傳：[{"date": "2026/06/03", "code": "2330", "name": "台積電", "location": "台北"}]
    失敗時回傳空 list（不拋出例外）。
    """
    today     = datetime.date.today()
    end_date  = today + datetime.timedelta(weeks=weeks)
    start_str = today.strftime("%Y%m%d")
    end_str   = end_date.strftime("%Y%m%d")

    try:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })
        # Step 1：GET 主頁面取得 Session Cookie
        sess.get(
            "https://mops.twse.com.tw/mops/web/t100sb01",
            timeout=15,
        )

        # Step 2：POST 查詢
        r = sess.post(
            "https://mops.twse.com.tw/mops/web/ajax_t100sb01",
            data={
                "encodeURIComponent": "1",
                "step":               "1",
                "firstin":            "1",
                "off":                "1",
                "keyword4":           "",
                "code1":              "",
                "TYPEK2":             "",
                "checkbtn":           "",
                "queryName":          "date",
                "inpuType":           "date",
                "startDate":          start_str,
                "endDate":            end_str,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      "https://mops.twse.com.tw/mops/web/t100sb01",
                "Origin":       "https://mops.twse.com.tw",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return []

        parser = _TableParser()
        parser.feed(r.text)

        events: list[dict] = []
        for row in parser.rows:
            if len(row) < 4:
                continue
            # 欄位順序（依 MOPS 2026 版）：序號, 日期, 時間, 股票代碼, 公司名稱, 地點/主辦
            # 嘗試識別日期欄（含 '/' 或 '-'）
            date_val = code_val = name_val = loc_val = ""
            for i, cell in enumerate(row):
                if ("/" in cell or "-" in cell) and len(cell) >= 8 and any(c.isdigit() for c in cell):
                    if not date_val:
                        date_val = cell
                elif cell.isdigit() and 4 <= len(cell) <= 6 and not code_val:
                    code_val = cell
                elif len(cell) >= 2 and any("一" <= c <= "鿿" for c in cell) and not name_val:
                    name_val = cell

            if date_val and (code_val or name_val):
                events.append({
                    "date":     date_val,
                    "code":     code_val,
                    "name":     name_val,
                    "location": row[-1] if len(row) >= 5 else "",
                })

        # 去重 + 排序
        seen = set()
        unique: list[dict] = []
        for ev in sorted(events, key=lambda x: x["date"]):
            key = (ev["date"], ev["code"] or ev["name"])
            if key not in seen:
                seen.add(key)
                unique.append(ev)

        return unique[:30]   # 最多 30 筆，避免 Discord 爆版

    except Exception as e:
        print(f"  ⚠️ MOPS 法說會抓取失敗：{e}")
        return []


# ── 美股財報（Yahoo Finance）──────────────────────────────────────

# 台股關聯度高的重要美股科技大型股（監控清單）
_KEY_US_TICKERS = [
    "NVDA", "AMD", "QCOM", "TSMC",    # 半導體（台積電上市 ADR）
    "AAPL", "MSFT", "GOOGL", "META",  # 大型科技
    "AMZN", "NFLX",                    # 電商/串流
    "TSM",                             # 台積電 ADR
    "INTC", "ASML",                    # 設備/晶片
]


def fetch_yahoo_earnings_week() -> list[dict]:
    """
    從 Yahoo Finance earnings calendar API 取本週財報。
    回傳：[{"date": "2026-06-03", "ticker": "NVDA", "name": "Nvidia", "eps_est": "6.71"}]
    """
    today     = datetime.date.today()
    # 本週一到周五
    monday    = today - datetime.timedelta(days=today.weekday())
    friday    = monday + datetime.timedelta(days=4)
    start_str = monday.strftime("%Y-%m-%d")
    end_str   = friday.strftime("%Y-%m-%d")

    try:
        url = (
            f"https://query1.finance.yahoo.com/v1/finance/trending/earningsCalendar"
            f"?startDate={start_str}&endDate={end_str}&size=100"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            earnings = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
            important = []
            for e in earnings:
                sym = e.get("symbol", "").upper()
                if sym in _KEY_US_TICKERS:
                    important.append({
                        "date":    e.get("earningsDate", ""),
                        "ticker":  sym,
                        "name":    e.get("shortName", sym),
                        "eps_est": e.get("epsEstimate", "N/A"),
                    })
            return important
    except Exception:
        pass

    # Fallback：用 Yahoo Finance 標準 earnings endpoint
    try:
        r2 = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/earningsCalendar",
            params={"startDate": start_str, "endDate": end_str, "size": 100},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if r2.status_code == 200:
            data = r2.json()
            all_e = (
                data.get("earningsCalendar", {}).get("result", [{}])[0].get("earnings", [])
            )
            important = [
                {
                    "date":    e.get("startdatetimetype", e.get("startdatetime", ""))[:10],
                    "ticker":  e.get("ticker", ""),
                    "name":    e.get("companyshortname", ""),
                    "eps_est": e.get("epsestimate", "N/A"),
                }
                for e in all_e
                if e.get("ticker", "").upper() in _KEY_US_TICKERS
            ]
            return important
    except Exception:
        pass

    return []


# ── Agent 主類別 ──────────────────────────────────────────────────

class EarningsAgent:
    """
    財報 / 法說會行事曆 Agent。
    run() 抓台股法說會 + 美股財報 → 發送 DISCORD_GLOBAL（08:00 早報）。
    """

    def run(self) -> dict:
        print("📅 [EarningsAgent] 抓取財報行事曆...")

        mops    = fetch_mops_investor_conf(weeks=2)
        us_earn = fetch_yahoo_earnings_week()

        print(f"  📋 台股法說會（2週）：{len(mops)} 場")
        print(f"  💹 美股財報（本週）：{len(us_earn)} 檔重點股")

        self._send(mops, us_earn)

        return {"mops": mops, "us_earnings": us_earn}

    def _send(self, mops: list[dict], us_earn: list[dict]) -> None:
        if not DISCORD_GLOBAL:
            print("  ⚠️ DISCORD_GLOBAL 未設定，跳過發送")
            return

        ts             = now_str()
        weekend_banner = "（週末版）" if is_weekend() else ""

        # 台股法說會文字
        if mops:
            lines = [
                f"`{ev['date']}` **{ev['name']}**（{ev['code']}）{ev.get('location','')}"
                for ev in mops[:15]   # 最多 15 筆
            ]
            mops_text = "\n".join(lines)
            if len(mops) > 15:
                mops_text += f"\n⋯ 還有 {len(mops)-15} 場（見 MOPS 官網）"
        else:
            mops_text = (
                "暫時無法自動取得法說會資料\n"
                "📎 [MOPS 法說會行事曆]"
                "(https://mops.twse.com.tw/mops/web/t100sb01)"
            )

        # 美股財報文字
        if us_earn:
            lines2 = [
                f"`{e['date'][-5:]}` **{e['ticker']}** {e['name']}  EPS Est.：{e['eps_est']}"
                for e in us_earn[:10]
            ]
            us_text = "\n".join(lines2)
        else:
            us_text = (
                "本週重點科技股財報請參閱：\n"
                "📎 [Earnings Whispers](https://www.earningswhispers.com/)\n"
                "📎 [Yahoo Finance Earnings](https://finance.yahoo.com/calendar/earnings)"
            )

        send_embed(DISCORD_GLOBAL, {
            "title":       f"📅 財報 / 法說會行事曆 {weekend_banner}| {ts}",
            "description": "本週 + 下週重要財報日期（台股法說會 + 美股大型股）",
            "color":       0xE74C3C,   # 紅色（提醒）
            "fields": [
                {"name": "🇹🇼 台股法說會（近 2 週）", "value": mops_text[:1024], "inline": False},
                {"name": "🇺🇸 美股重點財報（本週）",  "value": us_text[:1024],  "inline": False},
            ],
            "footer": {"text": "來源：MOPS 公開資訊觀測站 / Yahoo Finance Earnings Calendar"},
        })

        print("  ✅ [EarningsAgent] Discord 發送完成")
