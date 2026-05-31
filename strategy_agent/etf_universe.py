"""
strategy_agent/etf_universe.py — ETF 成分股宇宙

來源：
  0050    台灣50 ETF（元大）     — 固定50支，季度更新
  009816  元大台灣50等權重 ETF   — 成分同 0050
  00981A  野村台灣趨勢動能 ETF   — 主動型，每日披露，每日更新

使用方式：
  universe = get_universe()   # dict: {股票代號: ETF標籤}
"""

from __future__ import annotations
import datetime
import requests

# ── 0050 台灣50（2026Q2 版，季度更新）─────────────────────────────
# 資料來源：TWSE 定期公告，如有異動請手動更新或加 TWSE API 呼叫
ETF_0050: list[str] = [
    "2330",  # 台積電
    "2317",  # 鴻海
    "2454",  # 聯發科
    "2308",  # 台達電
    "2382",  # 廣達
    "3711",  # 日月光投控
    "2881",  # 富邦金
    "2882",  # 國泰金
    "2886",  # 兆豐金
    "2884",  # 玉山金
    "2891",  # 中信金
    "1303",  # 南亞
    "1301",  # 台塑
    "1216",  # 統一
    "2357",  # 華碩
    "2303",  # 聯電
    "2002",  # 中鋼
    "5880",  # 合庫金
    "2892",  # 第一金
    "2885",  # 元大金
    "2207",  # 和泰車
    "3045",  # 台灣大
    "2379",  # 瑞昱
    "2301",  # 光寶科
    "4938",  # 和碩
    "6669",  # 緯穎
    "2912",  # 統一超
    "2395",  # 研華
    "2408",  # 南亞科
    "3034",  # 聯詠
    "2883",  # 開發金
    "2887",  # 台新金
    "2603",  # 長榮
    "2327",  # 國巨
    "6505",  # 台塑化
    "1402",  # 遠東新
    "2880",  # 華南金
    "1326",  # 台化
    "2609",  # 陽明
    "2615",  # 萬海
    "3008",  # 大立光
    "5876",  # 上海商銀
    "2890",  # 永豐金
    "2823",  # 中壽
    "3037",  # 欣興
    "2353",  # 宏碁
    "1590",  # 亞德客-KY
    "6415",  # 矽力-KY
    "6770",  # 力積電
    "2376",  # 技嘉
]

# ── 009816 元大台灣50等權重（成分同 0050）────────────────────────
ETF_009816: list[str] = ETF_0050.copy()

# ── 00981A 野村台灣趨勢動能 ETF（備用清單，API 失敗時使用）─────────
# 此 ETF 為主動型，每日更新，以下為近期觀察到的常見成分
ETF_00981A_FALLBACK: list[str] = [
    "2330",  # 台積電
    "2454",  # 聯發科
    "2308",  # 台達電
    "2382",  # 廣達
    "6669",  # 緯穎
    "4938",  # 和碩
    "2379",  # 瑞昱
    "3034",  # 聯詠
    "3711",  # 日月光投控
    "2395",  # 研華
    "2303",  # 聯電
    "2327",  # 國巨
    "2357",  # 華碩
    "3008",  # 大立光
    "6415",  # 矽力-KY
    "2408",  # 南亞科
    "3037",  # 欣興
    "3045",  # 台灣大
    "2301",  # 光寶科
    "2353",  # 宏碁
    "2376",  # 技嘉
    "6770",  # 力積電
    "1590",  # 亞德客-KY
    "2317",  # 鴻海
    "2603",  # 長榮
]


# ── 00981A 每日成分抓取 ───────────────────────────────────────────

def _try_twse_etf_api(stock_no: str) -> list[str]:
    """
    嘗試從 TWSE ETF 成分股 API 取得最新成分股代號。
    endpoint：https://www.twse.com.tw/fund/ETF_CCNY
    """
    today = datetime.date.today().strftime("%Y%m%d")
    try:
        r = requests.get(
            "https://www.twse.com.tw/fund/ETF_CCNY",
            params={"response": "json", "date": today, "stockNo": stock_no},
            headers={"User-Agent": "Mozilla/5.0 (compatible; StockBot/1.0)"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        rows = data.get("data", [])
        # 格式：[["代號", "股票名稱", "持股比例%", ...], ...]
        symbols = []
        for row in rows:
            if row and row[0]:
                sym = str(row[0]).strip()
                if sym.isdigit() and len(sym) in (4, 5, 6):
                    symbols.append(sym)
        return symbols
    except Exception as e:
        print(f"  ⚠️  TWSE ETF API 失敗（{stock_no}）：{e}")
        return []


def _try_yuanta_fund_api(stock_no: str) -> list[str]:
    """
    備用：嘗試從基金公司 Open API 取得 00981A 成分（野村投信）。
    endpoint 因應官方 API 可能變動，此為實驗性實作。
    """
    try:
        # 野村投信可能的 API（若有更換請更新 URL）
        r = requests.get(
            "https://www.nomura-asset.com.tw/etf/api/portfolio",
            params={"fundCode": "00981A"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        # 格式視 API 而定，嘗試常見格式
        items = data.get("data", data.get("holdings", []))
        symbols = []
        for item in items:
            sym = str(item.get("stockCode", item.get("code", ""))).strip()
            if sym.isdigit():
                symbols.append(sym)
        return symbols
    except Exception:
        return []


def fetch_00981A() -> list[str]:
    """
    嘗試多個來源取得 00981A 今日成分股。
    失敗時回傳備用清單。
    """
    # 嘗試 1：TWSE API
    syms = _try_twse_etf_api("00981A")
    if syms:
        print(f"  ✅ 00981A 成分（TWSE API）：{len(syms)} 支")
        return syms

    # 嘗試 2：基金公司 API
    syms = _try_yuanta_fund_api("00981A")
    if syms:
        print(f"  ✅ 00981A 成分（基金公司 API）：{len(syms)} 支")
        return syms

    # 降級：備用清單
    print(f"  ⚠️  00981A API 無法取得，使用備用清單（{len(ETF_00981A_FALLBACK)} 支）")
    return ETF_00981A_FALLBACK


# ── 主函式 ────────────────────────────────────────────────────────

def get_universe() -> dict[str, str]:
    """
    整合所有 ETF 成分股，去重後回傳：
      {股票代號: "ETF來源標籤"}
    同時屬於多個 ETF 的股票，標籤以「/」分隔。
    """
    universe: dict[str, list[str]] = {}

    for sym in ETF_0050:
        universe.setdefault(sym, []).append("0050")

    for sym in ETF_009816:
        if "009816" not in universe.get(sym, []):
            universe.setdefault(sym, []).append("009816")

    comps_981a = fetch_00981A()
    for sym in comps_981a:
        if "00981A" not in universe.get(sym, []):
            universe.setdefault(sym, []).append("00981A")

    result = {sym: "/".join(tags) for sym, tags in universe.items()}
    print(f"  📊 策略選股宇宙：{len(result)} 支股票（"
          f"0050 {len(ETF_0050)} + 00981A {len(comps_981a)}，去重後）")
    return result
