"""
warrant.py  —  台灣上市認購(售)權證查詢與分析
資料來源：TWSE OpenAPI
  t187ap37_L  上市權證基本資料彙總表（含 Delta/IV/溢價率/槓桿）
  t187ap42_L  上市認購(售)權證每日成交資料
"""

import re
import time
import threading
import datetime
import requests

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/opendata"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 快取（附 Lock 避免同時下載）──────────────────────────────────
_basic_cache: dict      = {"data": None, "ts": 0}
_daily_cache: dict      = {"data": None, "ts": 0}
_vol_map_cache: dict    = {"data": None, "ts": 0}   # vol_map 單獨快取
_vol5d_cache: dict      = {"data": None, "ts": 0}   # 5日均量快取（1h）
_tpex_basic_cache: dict = {"data": None, "ts": 0}
_tpex_daily_cache: dict = {"data": None, "ts": 0}
_basic_lock             = threading.Lock()
_daily_lock             = threading.Lock()
_tpex_lock              = threading.Lock()


def _get_vol_map(daily: list | None = None) -> dict:
    """
    取得 {code: daily_row} 的 vol_map（TWSE+TPEX），快取 30min 避免每次重建。
    """
    now = time.time()
    if _vol_map_cache["data"] is not None and (now - _vol_map_cache["ts"]) < 1800:
        return _vol_map_cache["data"]
    twse_daily = daily if daily is not None else _fetch_daily()
    tpex_daily = _fetch_daily_tpex()
    vm = {r.get("權證代號", ""): r for r in twse_daily}
    vm.update({r.get("權證代號", ""): r for r in tpex_daily})
    _vol_map_cache["data"] = vm
    _vol_map_cache["ts"]   = time.time()
    return vm


def _get_vol5d_map() -> dict[str, float]:
    """
    取得 {code: 5日平均日成交張數}，快取 1h。
    從 warrant_daily DB 的歷史記錄計算；機器剛啟動無歷史時回傳空 dict。
    """
    now = time.time()
    if _vol5d_cache["data"] is not None and (now - _vol5d_cache["ts"]) < 3600:
        return _vol5d_cache["data"]
    try:
        from warrant_db import get_vol5d_map
        vm = get_vol5d_map()
        _vol5d_cache["data"] = vm
        _vol5d_cache["ts"]   = time.time()
        return vm
    except Exception:
        return {}


def _load_bs_cache_bulk(codes: list[str]) -> dict:
    """
    批量從 warrant_db.bs_cache 取資料，回傳 {code: row_dict}。
    避免在 _score() 裡逐筆查 DB。
    """
    if not codes:
        return {}
    try:
        from warrant_db import _conn as db_conn
        placeholders = ",".join("?" * len(codes))
        with db_conn() as c:
            rows = c.execute(
                f"SELECT * FROM bs_cache WHERE code IN ({placeholders})",
                codes,
            ).fetchall()
        return {r["code"]: dict(r) for r in rows}
    except Exception:
        return {}


def _fetch_basic_all() -> list:
    """
    載入全部權證基本資料，優先順序：
    1. 記憶體快取（1h）
    2. SQLite DB（24h 內更新）
    3. TWSE API（下載後存入 DB）
    4. SQLite DB（過期但有資料，作最後備援）
    """
    now = time.time()
    if _basic_cache["data"] is not None and (now - _basic_cache["ts"]) < 3600:
        return _basic_cache["data"]
    with _basic_lock:
        if _basic_cache["data"] is not None and (time.time() - _basic_cache["ts"]) < 3600:
            return _basic_cache["data"]

        # 嘗試從 DB 載入（24h 內更新視為有效）
        try:
            from warrant_db import load_basic_all, basic_age_hours, basic_count
            age = basic_age_hours()
            cnt = basic_count()
            if age < 24 and cnt > 0:
                db_data = load_basic_all()
                if db_data:
                    _basic_cache["data"] = db_data
                    _basic_cache["ts"]   = time.time()
                    print(f"✅ 從資料庫載入 {len(db_data)} 筆基本資料（{age:.1f}h 前更新）", flush=True)
                    return db_data
        except Exception as e:
            print(f"⚠️  讀取DB基本資料失敗：{e}", flush=True)

        # 向 TWSE API 下載
        try:
            r = requests.get(
                f"{TWSE_OPENAPI}/t187ap37_L",
                headers=HEADERS, timeout=120,
            )
            if r.status_code == 200:
                data = r.json()
                _basic_cache["data"] = data
                _basic_cache["ts"]   = time.time()
                if data:
                    print(f"✅ 權證基本資料已載入 {len(data)} 筆", flush=True)
                    print(f"🔍 全部欄位：{list(data[0].keys())}", flush=True)
                    # 存入 DB（背景執行避免阻塞）
                    try:
                        from warrant_db import save_basic
                        threading.Thread(target=save_basic, args=(data,), daemon=True).start()
                    except Exception:
                        pass
                return _basic_cache["data"]
        except Exception as e:
            print(f"⚠️  t187ap37_L 下載失敗：{e}", flush=True)

        # 最後備援：DB 過期資料也總比沒有好
        try:
            from warrant_db import load_basic_all
            stale = load_basic_all()
            if stale:
                print(f"⚠️  使用過期 DB 資料（{len(stale)} 筆）", flush=True)
                _basic_cache["data"] = stale
                _basic_cache["ts"]   = time.time()
                return stale
        except Exception:
            pass

    return _basic_cache["data"] or []


def _fetch_daily() -> list:
    """
    載入每日成交資料，優先順序：
    1. 記憶體快取（30min）
    2. SQLite DB（4h 內更新）
    3. TWSE API（下載後存入 DB）
    """
    now = time.time()
    if _daily_cache["data"] is not None and (now - _daily_cache["ts"]) < 1800:
        return _daily_cache["data"]
    with _daily_lock:
        if _daily_cache["data"] is not None and (time.time() - _daily_cache["ts"]) < 1800:
            return _daily_cache["data"]

        # 嘗試從 DB 載入（4h 內視為有效）
        try:
            from warrant_db import load_daily_today, daily_age_hours
            if daily_age_hours() < 4:
                db_data = load_daily_today()
                if db_data:
                    _daily_cache["data"] = db_data
                    _daily_cache["ts"]   = time.time()
                    return db_data
        except Exception:
            pass

        # 向 TWSE API 下載
        try:
            r = requests.get(
                f"{TWSE_OPENAPI}/t187ap42_L",
                headers=HEADERS, timeout=30,
            )
            if r.status_code == 200:
                data = r.json()
                _daily_cache["data"] = data
                _daily_cache["ts"]   = time.time()
                try:
                    from warrant_db import save_daily
                    threading.Thread(target=save_daily, args=(data,), daemon=True).start()
                except Exception:
                    pass
                _vol_map_cache["ts"] = 0   # 讓 vol_map 跟著失效重建
                return _daily_cache["data"]
        except Exception as e:
            print(f"⚠️  t187ap42_L 下載失敗：{e}", flush=True)
    return _daily_cache["data"] or []


# ── TPEX（上櫃）資料 ──────────────────────────────────────────────

def _normalize_tpex_basic(row: dict) -> dict:
    """將 TPEX 權證發行基本資料正規化為 TWSE 相容格式"""
    ratio_raw = (row.get("LatestExerciseRatio")
                 or row.get("Latest ExerciseRatio", ""))
    try:
        ratio_per_k = str(round(float(ratio_raw) * 1000, 4))
    except Exception:
        ratio_per_k = ""
    return {
        "權證代號":                            row.get("Code", ""),
        "權證簡稱":                            row.get("Name", ""),
        "最後交易日":                          row.get("ExpiryDate", ""),
        "最新履約價格(元)/履約指數":            row.get("LatestExercisePrice", ""),
        "最新標的的履約配發數量(每仟單位權證)": ratio_per_k,
        "標的證券/指數":                       row.get("UnderlyingStockCode", ""),
        "權證類型":                            row.get("Type", ""),
        "發行單位數量(仟單位)":                str(row.get("InitialIssuance", "")),
        "_source":                            "tpex",
    }


def _normalize_tpex_daily(row: dict) -> dict:
    """將 TPEX 每日成交資料正規化為 TWSE 相容格式"""
    return {
        "權證代號": row.get("Code", ""),
        "成交張數": row.get("TradeVol.", row.get("TradeVol", "")),
        "收盤價":   row.get("Close", ""),
        "_source":  "tpex",
    }


def _fetch_basic_all_tpex() -> list:
    """載入 TPEX 上櫃權證基本資料（記憶體快取 1h）"""
    now = time.time()
    if _tpex_basic_cache["data"] is not None and (now - _tpex_basic_cache["ts"]) < 3600:
        return _tpex_basic_cache["data"]
    with _tpex_lock:
        if _tpex_basic_cache["data"] is not None and (time.time() - _tpex_basic_cache["ts"]) < 3600:
            return _tpex_basic_cache["data"]
        try:
            r = requests.get(
                f"{TPEX_OPENAPI}/tpex_warrant_issue",
                headers=HEADERS, timeout=60,
            )
            if r.status_code == 200:
                raw  = r.json()
                data = [_normalize_tpex_basic(row) for row in raw if row.get("Code")]
                _tpex_basic_cache["data"] = data
                _tpex_basic_cache["ts"]   = time.time()
                print(f"✅ TPEX 上櫃權證基本資料已載入 {len(data)} 筆", flush=True)
                # 寫入 DB，讓 SQL 搜尋路徑也能查到上櫃權證
                try:
                    from warrant_db import save_basic as _save_basic
                    threading.Thread(
                        target=_save_basic, args=(data,), kwargs={"source": "tpex"}, daemon=True
                    ).start()
                except Exception:
                    pass
                return data
        except Exception as e:
            print(f"⚠️  TPEX 基本資料下載失敗：{e}", flush=True)
    return _tpex_basic_cache["data"] or []


def _fetch_daily_tpex() -> list:
    """載入 TPEX 上櫃權證每日成交資料（記憶體快取 30min）"""
    now = time.time()
    if _tpex_daily_cache["data"] is not None and (now - _tpex_daily_cache["ts"]) < 1800:
        return _tpex_daily_cache["data"]
    try:
        r = requests.get(
            f"{TPEX_OPENAPI}/tpex_warrant_daily_quts",
            headers=HEADERS, timeout=30,
        )
        if r.status_code == 200:
            raw  = r.json()
            data = [_normalize_tpex_daily(row) for row in raw if row.get("Code")]
            _tpex_daily_cache["data"] = data
            _tpex_daily_cache["ts"]   = time.time()
            return data
    except Exception as e:
        print(f"⚠️  TPEX 日成交下載失敗：{e}", flush=True)
    return _tpex_daily_cache["data"] or []


def prewarm_cache():
    """Bot 啟動時在背景預先下載，確保第一個使用者不需等待"""
    def _task():
        print("🔄 預熱權證快取中…", flush=True)
        _fetch_basic_all()
        _fetch_daily()
        _fetch_basic_all_tpex()
        _fetch_daily_tpex()
        print("✅ 權證快取預熱完成（TWSE+TPEX）", flush=True)
    threading.Thread(target=_task, daemon=True).start()


# ── mis.twse.com.tw 即時補充資料 ─────────────────────────────────

def _parse_mis_entry(m: dict) -> dict:
    """將 MIS msgArray 單筆 dict 解析為統一格式"""
    def _v(k):
        val = m.get(k, "")
        return val if val and val not in ("-", "--") else ""

    live = _v("z")
    prev = _v("y")

    def _parse_book(price_str: str, vol_str: str) -> list[tuple[str, str]]:
        ps = [p for p in price_str.split("_") if p and p not in ("-", "--")]
        vs = [v for v in vol_str.split("_")   if v and v not in ("-", "--")]
        return list(zip(ps[:5], vs[:5]))

    bid_book = _parse_book(_v("b"), _v("g"))
    ask_book = _parse_book(_v("a"), _v("f"))

    return {
        "price":      live or prev,
        "prev_close": prev,
        "open":       _v("o"),
        "high":       _v("h"),
        "low":        _v("l"),
        "volume":     _v("v"),
        "bid":        bid_book[0][0] if bid_book else "",
        "ask":        ask_book[0][0] if ask_book else "",
        "bid_vol":    bid_book[0][1] if bid_book else "",
        "ask_vol":    ask_book[0][1] if ask_book else "",
        "bid_book":   bid_book,
        "ask_book":   ask_book,
        "name":       m.get("n", ""),
        "is_live":    bool(live),
    }


def _fetch_mis_batch(code_exchange_pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """
    批量取得 MIS 即時行情（一次 HTTP 請求取多筆）。
    code_exchange_pairs: [(code, exchange), ...]  exchange="tw" 或 "otc"
    回傳 {code: mis_dict}，最多建議 40 支/批。
    """
    if not code_exchange_pairs:
        return {}
    result: dict = {}
    BATCH = 40
    for start in range(0, len(code_exchange_pairs), BATCH):
        batch = code_exchange_pairs[start : start + BATCH]
        ex_ch = "|".join(f"{ex}_{c}.tw" for c, ex in batch)
        try:
            r = requests.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
                f"?ex_ch={ex_ch}&json=1&delay=0",
                headers=HEADERS, timeout=5,   # 5s — MIS 通常 <1s，超時就放棄
            )
            if r.status_code == 200:
                for m in r.json().get("msgArray", []):
                    c = m.get("c", "")
                    if c:
                        result[c] = _parse_mis_entry(m)
        except Exception as e:
            print(f"MIS 批量錯誤：{e}", flush=True)
    return result


def _fetch_mis(code: str, exchange: str = "tw") -> dict:
    """
    取得單支股票/權證的 MIS 即時行情（不自動 retry 另一交易所）。
    需要 fallback 的呼叫方請自行用 _fetch_mis_batch 批量處理。
    """
    result = _fetch_mis_batch([(code, exchange)])
    return result.get(code, {})


def _calc_premium(warrant_price: float, stock_price: float,
                  strike: float, ratio: float, is_call: bool = True) -> str:
    """
    溢價率 = (W/ratio + K - S) / S × 100  （認購）
           = (W/ratio + S - K) / S × 100  （認售）
    """
    try:
        if ratio <= 0 or stock_price <= 0:
            return ""
        if is_call:
            prem = (warrant_price / ratio + strike - stock_price) / stock_price * 100
        else:
            prem = (warrant_price / ratio + stock_price - strike) / stock_price * 100
        return f"{prem:.2f}"
    except Exception:
        return ""


def _calc_leverage(warrant_price: float, stock_price: float,
                   delta: float, ratio: float) -> str:
    """有效槓桿 = |Delta| × 股價 / (W / ratio)"""
    try:
        if warrant_price <= 0 or ratio <= 0:
            return ""
        lev = abs(delta) * stock_price / (warrant_price / ratio)
        return f"{lev:.2f}"
    except Exception:
        return ""


# ── 生命週期狀態 ──────────────────────────────────────────────────

def _warrant_status(row: dict) -> str:
    """
    回傳 'active' / 'near_expiry'（≤30天）/ 'expired'
    """
    exp = _expiry(row)
    if not exp:
        return "unknown"
    days = _days_to_expiry(exp)
    if days <= 0:
        return "expired"
    if days <= 30:
        return "near_expiry"
    return "active"


# ── 股票代號 / 中文名稱查詢 ───────────────────────────────────────

def _get_tw_name(code: str) -> str:
    """從 mis.twse.com.tw 取得台股中文名稱"""
    try:
        r = requests.get(
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch=tw_{code}.tw&json=1&delay=0",
            headers=HEADERS, timeout=6,
        )
        if r.status_code == 200:
            msgs = r.json().get("msgArray", [])
            if msgs:
                name = msgs[0].get("n", "").strip()
                if name:
                    return name
    except Exception:
        pass
    return ""


def lookup_stock(query: str) -> tuple[str, str]:
    """
    輸入代號(2330)或中文名(台積電) → (code, zh_name)
    優先用 TWSE MIS 取中文名；fallback Yahoo Finance
    """
    q = query.strip()

    # 純數字 4-6 碼 → 嘗試取中文名
    if re.match(r"^\d{4,6}$", q):
        name = _get_tw_name(q)
        if name:
            return q, name
        # fallback Yahoo Finance
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{q}.TW?interval=1d&range=1d",
                headers=HEADERS, timeout=8,
            )
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                return q, meta.get("shortName", q)
        except Exception:
            pass
        return q, q

    # 中文/英文名 → Yahoo Finance 搜尋
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v1/finance/search"
            f"?q={requests.utils.quote(q)}&newsCount=0&quotesCount=5&region=TW&lang=zh-TW",
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            for item in r.json().get("quotes", []):
                sym = item.get("symbol", "")
                if re.match(r"^\d{4}\.TW$", sym):
                    code = sym.replace(".TW", "")
                    # 用 TWSE MIS 取中文名
                    zh = _get_tw_name(code)
                    if not zh:
                        zh = item.get("longname") or item.get("shortname", code)
                    return code, zh
    except Exception:
        pass

    return q, q


# ── 日期處理 ──────────────────────────────────────────────────

def _parse_date(s: str) -> datetime.date | None:
    s = str(s).strip().replace("/", "").replace("-", "")
    try:
        if len(s) == 7:          # 民國年 yyyMMdd
            y = int(s[:3]) + 1911
            m, d = int(s[3:5]), int(s[5:7])
        elif len(s) == 8:        # 西元年 yyyyMMdd
            y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        else:
            return None
        return datetime.date(y, m, d)
    except Exception:
        return None


def _days_to_expiry(expiry_str: str) -> int:
    d = _parse_date(expiry_str)
    if d is None:
        return 0
    return max(0, (d - datetime.date.today()).days)


# ── 欄位自動偵測（相容不同欄位名稱版本）────────────────────────

def _field(row: dict, *candidates) -> str:
    for k in candidates:
        if k in row and str(row[k]).strip():
            return str(row[k]).strip()
    return ""


def _underlying_code(row: dict) -> str:
    # 實際欄位：'標的證券/指數'，值可能是 "2330" 或 "台積電" 或 "2330台積電"
    raw = _field(row,
        "標的證券/指數",
        "標的有價證券代號", "標的股票代號", "標的代號",
        "標的有価証券代號", "標的",
    )
    if raw:
        # 嘗試抽取純4位數股票代碼
        m = re.search(r"\b(\d{4})\b", raw)
        if m:
            return m.group(1)
        return raw
    return ""


def _underlying_name(row: dict) -> str:
    # 嘗試從 '標的證券/指數' 取中文名稱部分
    raw = _field(row,
        "標的證券/指數",
        "標的有價證券名稱", "標的股票名稱", "標的名稱",
    )
    if raw:
        # 移除數字只留中文部分
        name = re.sub(r"\d+", "", raw).strip()
        return name if name else raw
    return ""


def _expiry(row: dict) -> str:
    return _field(row, "最後交易日", "到期日期", "到期日", "下市日期", "履約截止日", "期限")


def _strike(row: dict) -> str:
    # 優先取最新履約價（已調整），fallback 原始
    return _field(row,
        "最新履約價格(元)/履約指數",
        "原始履約價格(元)/履約指數",
        "履約價格", "行使價格", "履約價",
    )


def _ratio(row: dict) -> str:
    # 實際欄位：最新標的的履約配發數量(每仟單位權證)（注意有兩個「的」）
    return _field(row,
        "最新標的的履約配發數量(每仟單位權證)",
        "最新標的履約配發數量(每仟單位權證)",
        "最新標的的履約配發數量（每仟單位權證）",
        "最新標的履約配發數量（每仟單位權證）",
        "行使比例", "換股比例",
    )


def _delta(row: dict) -> str:
    return _field(row, "Delta", "delta", "DELTA", "Greeks_Delta", "避險比率")


def _iv(row: dict) -> str:
    return _field(row, "隱含波動率", "IV", "隱波率", "波動率", "隱含波動率(%)")


def _premium(row: dict) -> str:
    return _field(row, "溢價率", "溢價", "Premium", "溢價率(%)")


def _leverage(row: dict) -> str:
    return _field(row,
        "實際槓桿倍數", "實際槓桿", "有效槓桿", "槓桿比例", "槓桿倍數",
        "槓桿比率", "實際槓桿比率",
    )


def _prev_close(row: dict) -> str:
    return _field(row, "前一日收盤價", "昨收", "昨收盤", "收盤價", "前收", "參考價")


def _issued_units(row: dict) -> str:
    return _field(row, "發行單位數量(仟單位)", "發行單位數量（仟單位）", "發行張數", "發行量")


def _wtype(row: dict) -> str:
    t = _field(row, "權證類型", "行使類型", "認購認售", "類型", "Type")
    if not t:
        name = row.get("權證簡稱", row.get("權證名稱", ""))
        return "認購" if "購" in name else ("認售" if "售" in name else "未知")
    # 統一格式
    if "認購" in t or "購" in t:
        return "認購"
    if "認售" in t or "售" in t:
        return "認售"
    return t


# ── 評分 ──────────────────────────────────────────────────────

def _score(row: dict, vol_map: dict,
           bs_cache_map: dict | None = None,
           vol5d_map: dict | None = None) -> float:
    """
    bs_cache_map: {code: bs_cache_row} 可選傳入，避免重複查 DB。
    vol5d_map   : {code: avg_5d_vol}   可選傳入，5日均量優先於今日量評分。
    """
    score = 0.0
    code  = row.get("權證代號", "")

    # 生命週期：到期/近到期排除
    status = _warrant_status(row)
    if status == "expired":
        return -999.0
    days = _days_to_expiry(_expiry(row)) if _expiry(row) else 0
    if days < 90:
        return -999.0
    if   90 <= days <= 180: score += 30.0
    elif days > 180:        score += 20.0

    # 流動性：優先用 5日均量，無則用今日量
    vol5d    = float(vol5d_map.get(code, 0)) if vol5d_map else 0.0
    vol_today = int(vol_map.get(code, {}).get("成交張數", 0) or 0)
    vol = vol5d if vol5d > 0 else float(vol_today)

    if   vol > 500_000: score += 30.0   # 超高流動
    elif vol > 100_000: score += 20.0   # 高流動
    elif vol > 20_000:  score += 10.0   # 中流動
    elif vol > 1_000:   score +=  2.0   # 低流動
    else:               score -= 10.0   # 無/極低流動

    # BS 快取（含溢價率、Delta、槓桿）— 優先使用，fallback TWSE 欄位
    bs = None
    if bs_cache_map is not None:
        bs = bs_cache_map.get(code)
    else:
        try:
            from warrant_db import get_bs_cache
            bs = get_bs_cache(code)
        except Exception:
            pass

    prem_raw  = (bs["premium_pct"] if bs and bs.get("premium_pct") is not None
                 else _premium(row).replace("%", "") if _premium(row) else None)
    delta_raw = (bs["delta"]    if bs and bs.get("delta")    is not None else _delta(row))
    lev_raw   = (bs["leverage"] if bs and bs.get("leverage") is not None else _leverage(row))

    # 溢價率（低為佳）
    try:
        prem = float(prem_raw)
        if   prem < 0:   score -= 30.0
        elif prem < 3:   score += 20.0
        elif prem < 6:   score += 12.0
        elif prem < 10:  score +=  5.0
        elif prem < 20:  pass
        else:            score -= 15.0
    except Exception:
        pass

    # Delta（認購 0.3~0.6 最佳）
    try:
        delta = abs(float(delta_raw))
        if   0.3 <= delta <= 0.6:                        score += 15.0
        elif 0.2 <= delta < 0.3 or 0.6 < delta <= 0.75: score +=  8.0
    except Exception:
        pass

    # 有效槓桿（5~15 倍最佳）
    try:
        lev = float(lev_raw)
        if   5 <= lev <= 15:              score += 15.0
        elif 3 <= lev < 5 or 15 < lev <= 25: score +=  7.0
        elif lev > 25:                    score -=  5.0
    except Exception:
        pass

    # 異常警示（BS 已標記的）
    if bs and bs.get("anomaly"):
        score -= 20.0

    return score


# ── 格式化單筆 ────────────────────────────────────────────────

def _na(v: str) -> str:
    return v if v else "—"


def _fmt_warrant(row: dict, vol_map: dict, rank: int | None = None,
                 mis_data: dict | None = None,
                 stock_price: float = 0.0,
                 stock_mis: dict | None = None) -> str:
    """
    格式化單筆權證。
    mis_data   — 權證的 _fetch_mis() 結果
    stock_price — 標的股票現價（用於計算溢價率/槓桿）
    stock_mis  — 標的股票的 _fetch_mis() 結果（顯示股價）
    """
    code   = row.get("權證代號", "?")
    name   = row.get("權證簡稱", row.get("權證名稱", "?"))
    wt     = _wtype(row)
    exp    = _expiry(row)
    days   = _days_to_expiry(exp) if exp else "?"
    und_c  = _na(_underlying_code(row))
    und_n  = _na(_underlying_name(row))
    issued = _na(_issued_units(row))

    # 履約價 & 行使比例（靜態資料）
    strike_s = _strike(row)
    ratio_s  = _ratio(row)
    try:
        ratio_f = float(ratio_s) / 1000.0
    except Exception:
        ratio_f = 0.0

    # 權證昨收/現價
    # fallback 鏈：MIS昨收 → t187ap42_L收盤價 → TWSE靜態欄位
    mis         = mis_data or {}
    daily_row   = vol_map.get(code, {})
    daily_close = str(daily_row.get("收盤價", "") or "").strip()
    close       = mis.get("prev_close") or daily_close or _prev_close(row) or "—"
    curr_p      = mis.get("price") or close
    bid         = mis.get("bid", "")
    ask         = mis.get("ask", "")
    mis_vol     = mis.get("volume", "")
    is_live     = mis.get("is_live", False)

    # 5日均量（模組快取，不影響外部傳參）
    vol5d_map_inner = _get_vol5d_map()
    vol5d_val       = vol5d_map_inner.get(code, 0.0)

    # ── Black-Scholes 計算（IV / Greeks）────────────────────────
    delta_s      = _delta(row)
    iv_s         = _iv(row)
    theta_s      = ""
    gamma_s      = ""
    lev_s        = ""
    prem_s       = ""
    iv_pct_s     = ""
    grade_s      = ""
    bs_ok        = False
    anomaly_flag = ""
    bs           = {}

    if stock_price > 0 and curr_p and strike_s and ratio_f > 0:
        try:
            from warrant_bs import full_calc, calc_grade
            w_p = float(curr_p)
            k   = float(strike_s)
            d_i = int(days) if isinstance(days, int) else 0
            if w_p > 0 and k > 0 and d_i > 0:
                is_call = "購" in wt
                bs = full_calc(stock_price, k, d_i, w_p, ratio_f, is_call)
                if bs:
                    if "iv" in bs:
                        iv_s  = f"{bs['iv']:.1f}"
                        bs_ok = True
                    if "delta"    in bs: delta_s = str(bs["delta"])
                    if "theta"    in bs: theta_s = str(bs["theta"])
                    if "gamma"    in bs: gamma_s = str(bs["gamma"])
                    if "leverage" in bs: lev_s   = f"{bs['leverage']:.2f}"
                    if bs.get("premium_pct") is not None:
                        prem_s = f"{bs['premium_pct']:.2f}"
                    anomaly_flag = bs.get("anomaly", "")
                    # 綜合評級
                    _g, _gl = calc_grade(bs, d_i)
                    grade_s = f"{_g} {_gl}"
                    # IV Percentile
                    if bs_ok:
                        try:
                            from warrant_db import calc_iv_percentile
                            pct = calc_iv_percentile(code)
                            if pct is not None:
                                iv_pct_s = f"{pct:.0f}%ile"
                        except Exception:
                            pass
        except Exception:
            pass

    # Fallback：若 BS 算不出（缺股價等），嘗試舊版手算
    if not bs_ok:
        # 溢價率：API 或手算
        prem_s = _premium(row)
        if not prem_s:
            try:
                w = float(curr_p); k = float(strike_s)
                if w > 0 and k > 0 and stock_price > 0 and ratio_f > 0:
                    is_call = "購" in wt
                    prem_s = _calc_premium(w, stock_price, k, ratio_f, is_call)
            except Exception:
                pass

        # Delta：歷史資料庫估算
        if not delta_s:
            try:
                from warrant_db import calc_delta as db_calc_delta, get_history_days
                est = db_calc_delta(code)
                if est is not None:
                    h_days = get_history_days(code)
                    delta_s = f"≈{est:.3f}（{h_days}日歷史估算）"
            except Exception:
                pass

        # 槓桿：有 Delta 用有效槓桿，否則用價格槓桿
        lev_s = _leverage(row)
        if not lev_s:
            try:
                w = float(curr_p)
                d = abs(float(str(delta_s).split("（")[0].replace("≈", ""))) if delta_s else 0
                if d > 0 and w > 0 and ratio_f > 0:
                    lev_s = _calc_leverage(w, stock_price, d, ratio_f)
                elif w > 0 and ratio_f > 0 and stock_price > 0:
                    il = stock_price * ratio_f / w
                    lev_s = f"≈{il:.1f}（價格槓桿）"
            except Exception:
                pass

    # vol_today_disp 在後面的 lines 裡計算，此行廢棄但保留 vol for _score fallback
    vol = mis_vol or vol_map.get(code, {}).get("成交張數", "N/A")
    prefix = f"#{rank} " if rank else ""
    status = _warrant_status(row)
    status_tag = " ⚠️即將到期" if status == "near_expiry" else ""

    # 標的股票價格
    s_mis    = stock_mis or {}
    sp_str   = s_mis.get("price", "")
    sp_live  = s_mis.get("is_live", False)
    sp_label = "現價" if sp_live else "昨收"

    # 五檔造市商資訊（盤中才顯示）
    bid_book = mis.get("bid_book", [])
    ask_book = mis.get("ask_book", [])
    orderbook_lines = []
    if is_live and (bid_book or ask_book):
        orderbook_lines.append("  【五檔】  賣 ← 買")
        for i in range(max(len(bid_book), len(ask_book))):
            b = bid_book[i] if i < len(bid_book) else ("—", "—")
            a = ask_book[i] if i < len(ask_book) else ("—", "—")
            orderbook_lines.append(f"  {b[0]}×{b[1]}張  |  {a[0]}×{a[1]}張")

    grade_tag = f"  【評級 {grade_s}】" if grade_s else ""
    lines = [
        f"{prefix}**{name}**（{code}） — {wt}{status_tag}{grade_tag}",
        f"  標的：{und_n}（{und_c}）",
    ]
    if sp_str:
        lines.append(f"  標的{sp_label}：{sp_str} 元")

    w_label = "現價" if is_live else "昨收"
    w_display = curr_p if is_live else close
    lines.append(f"  權證{w_label}：{w_display} 元")

    if is_live and curr_p != close:
        bid_disp = f"{bid}×{mis.get('bid_vol','')}張" if bid else "—"
        ask_disp = f"{ask}×{mis.get('ask_vol','')}張" if ask else "—"
        lines.append(f"  買：{bid_disp}  賣：{ask_disp}")

    lines += orderbook_lines

    # 成交量顯示：今日 + 5日均量（若有）
    vol_today_disp = mis_vol or vol_map.get(code, {}).get("成交張數", "N/A")
    if vol5d_val and vol5d_val > 0:
        vol_disp = f"{vol_today_disp}張（5日均量 {vol5d_val:,.0f}張）"
    else:
        vol_disp = f"{vol_today_disp}張"

    lines += [
        f"  到期日：{exp}（剩 {days} 天）",
        f"  履約價：{_na(strike_s)} 元 | 行使比例：{_na(ratio_s)} 股/千單位",
        f"  IV：{_na(iv_s)}%{'(BS)' if bs_ok else ''}{'  '+iv_pct_s if iv_pct_s else ''} | Delta：{_na(delta_s)} | Theta：{_na(theta_s)}/日",
        f"  溢價率：{_na(prem_s)}% | 有效槓桿：{_na(lev_s)}倍",
        f"  發行量：{issued} 仟 | 今日成交：{vol_disp}",
    ]
    if anomaly_flag:
        lines.append(f"  ⚠️ 異常警示：{anomaly_flag}")
    return "\n".join(lines)


# ── 搜尋特定股票的好權證 ─────────────────────────────────────

def search_warrants(stock_input: str, top_n: int = 5) -> str:
    code, zh_name = lookup_stock(stock_input)
    today = datetime.date.today()
    today_s = today.strftime("%Y%m%d")

    # ── 優先用 SQL 查詢（快取DB中已有索引的 und_code 欄位）──────
    matched: list = []
    try:
        from warrant_db import _conn as db_conn
        import json as _json
        with db_conn() as c:
            rows = c.execute(
                "SELECT raw FROM warrant_basic WHERE und_code=? AND expiry > ? AND status != 'expired'",
                (code, today_s),
            ).fetchall()
            matched = [_json.loads(r["raw"]) for r in rows]
    except Exception:
        pass

    # Fallback：記憶體全量過濾（DB 尚未建立時，含 TPEX）
    if not matched:
        basic_all = _fetch_basic_all() + _fetch_basic_all_tpex()
        if not basic_all:
            return "❌ 無法取得權證基本資料，請稍後再試"
        matched = [
            r for r in basic_all
            if _underlying_code(r) == code
            and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        ]

    def _wname(r):
        return r.get("權證簡稱", r.get("權證名稱", ""))

    # 中文名稱 fallback（前3字，含 TPEX）
    if not matched and re.search(r"[一-鿿]", zh_name):
        short = zh_name[:3]
        basic_all = _fetch_basic_all() + _fetch_basic_all_tpex()
        matched = [
            r for r in basic_all
            if short in _wname(r)
            and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        ]

    if not matched:
        return f"❌ 找不到 {zh_name}（{code}）的有效認購權證"

    # 只取認購
    calls = [r for r in matched if "購" in _wtype(r) or "購" in _wname(r)]
    if not calls:
        calls = matched

    # ── vol_map：使用快取，避免每次重建 ─────────────────────────
    daily   = _fetch_daily()
    vol_map = _get_vol_map(daily)

    # ── 5日均量篩選（優先），fallback 今日量 ──────────────────
    vol5d_map = _get_vol5d_map()
    MIN_VOL5D = 500    # 5日均量 >= 500張/日
    MIN_VOL1D = 1000   # 或今日量 >= 1000張

    def _has_liquidity(r):
        c5 = r.get("權證代號", "")
        v5 = vol5d_map.get(c5, 0.0)
        v1 = int(vol_map.get(c5, {}).get("成交張數", 0) or 0)
        return v5 >= MIN_VOL5D or v1 >= MIN_VOL1D

    liquid = [r for r in calls if _has_liquidity(r)]
    if liquid:
        calls = liquid

    # ── 批量載入 BS Cache（_score 需要 BS 資料評分）───────────
    all_codes = [r.get("權證代號", "") for r in calls]
    bs_cache_map = _load_bs_cache_bulk(all_codes)

    ranked = sorted(calls,
                    key=lambda r: _score(r, vol_map, bs_cache_map, vol5d_map),
                    reverse=True)[:top_n]

    # ── 批量 MIS：標的股票 + 所有上榜權證即時報價 ─────────────
    warrant_pairs = [
        (r.get("權證代號", ""), "otc" if r.get("_source") == "tpex" else "tw")
        for r in ranked
    ]
    all_pairs = [(code, "tw")] + warrant_pairs
    mis_batch = _fetch_mis_batch(all_pairs)

    # 股票價格：MIS tw → MIS otc → Yahoo Finance（最後防線）
    mis_s = mis_batch.get(code, {})
    if not mis_s.get("price"):
        otc_batch = _fetch_mis_batch([(code, "otc")])
        mis_s = otc_batch.get(code, {})

    stock_price = 0.0
    try:
        stock_price = float(mis_s.get("price", 0) or 0)
    except Exception:
        pass

    if stock_price <= 0:
        try:
            yf = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.TW"
                "?interval=1d&range=3d",
                headers=HEADERS, timeout=8,
            )
            if yf.status_code == 200:
                closes = yf.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                c = next((x for x in reversed(closes) if x), None)
                if c:
                    stock_price = float(c)
                    mis_s = {"price": f"{c:.2f}", "is_live": False}
        except Exception:
            pass

    lines = [f"**{zh_name}（{code}）認購權證前{len(ranked)}名**\n"]
    for i, row in enumerate(ranked, 1):
        w_code = row.get("權證代號", "")
        mis_w  = mis_batch.get(w_code, {})
        lines.append(_fmt_warrant(row, vol_map, rank=i,
                                  mis_data=mis_w,
                                  stock_price=stock_price, stock_mis=mis_s))
        lines.append("")
    return "\n".join(lines)


# ── 分析指定權證 ─────────────────────────────────────────────

def analyze_warrant(warrant_code: str) -> tuple[str, str]:
    wc        = warrant_code.strip().upper()
    twse_all  = _fetch_basic_all()
    tpex_all  = _fetch_basic_all_tpex()
    basic_all = twse_all + tpex_all
    daily     = _fetch_daily()

    target = next(
        (r for r in basic_all if r.get("權證代號", "").upper() == wc), None
    )
    if not target:
        return (f"❌ 找不到權證代號 `{wc}`"
                f"（已查詢 {len(twse_all)} 筆上市 + {len(tpex_all)} 筆上櫃）"), ""

    vol_map  = _get_vol_map(daily)
    und_code = _underlying_code(target)

    # 補充即時資料：一次批次呼叫取得權證 + 標的股票價格
    w_exchange = "otc" if target.get("_source") == "tpex" else "tw"
    pairs: list[tuple[str, str]] = [(wc, w_exchange)]
    if und_code:
        pairs.append((und_code, "tw"))
    mis_batch   = _fetch_mis_batch(pairs)
    mis_w       = mis_batch.get(wc, {})
    mis_s: dict = {}
    stock_price = 0.0
    if und_code:
        mis_s = mis_batch.get(und_code, {})
        # otc fallback（上櫃標的）
        if not mis_s.get("price"):
            otc_b = _fetch_mis_batch([(und_code, "otc")])
            mis_s = otc_b.get(und_code, {})
        try:
            stock_price = float(mis_s.get("price", 0) or 0)
        except Exception:
            pass
        if stock_price <= 0:
            try:
                yf = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{und_code}.TW"
                    "?interval=1d&range=3d",
                    headers=HEADERS, timeout=8,
                )
                if yf.status_code == 200:
                    closes = yf.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    c = next((x for x in reversed(closes) if x), None)
                    if c:
                        stock_price = float(c)
                        mis_s = {"price": f"{c:.2f}", "is_live": False,
                                 "prev_close": f"{c:.2f}"}
            except Exception:
                pass

    print(f"🔍 分析權證 {wc}: und_code={und_code}, stock_price={stock_price}, "
          f"warrant_price={mis_w.get('price','?')}", flush=True)

    # 記錄價格快照（供 Delta 估算、IV 歷史、BS 快取）
    try:
        ratio_s_rec = _ratio(target)
        ratio_f_rec = float(ratio_s_rec) / 1000.0 if ratio_s_rec else 1.0
        w_p = float(mis_w.get("price", 0) or 0)
        if w_p > 0 and und_code:
            from warrant_bs import full_calc
            from warrant_db import record_price, save_bs_cache
            is_call_rec = "購" in _wtype(target)
            strike_rec  = _strike(target)
            days_rec    = _days_to_expiry(_expiry(target))
            bs_rec: dict = {}
            if stock_price > 0 and strike_rec and days_rec > 0:
                try:
                    k_rec = float(strike_rec)
                    bs_rec = full_calc(stock_price, k_rec, days_rec,
                                       w_p, ratio_f_rec, is_call_rec)
                    if bs_rec:
                        save_bs_cache(wc, bs_rec)
                except Exception:
                    pass
            record_price(
                wc, und_code, w_p, stock_price, ratio_f_rec,
                iv_bs=bs_rec.get("iv"), delta_bs=bs_rec.get("delta"),
                premium_pct=bs_rec.get("premium_pct"),
            )
    except Exception:
        pass

    target_summary = _fmt_warrant(target, vol_map,
                                  mis_data=mis_w, stock_price=stock_price,
                                  stock_mis=mis_s)

    # 從名稱推出標的前3字（例：台積電凱基→台積電）
    t_name    = target.get("權證簡稱", target.get("權證名稱", ""))
    und_short = t_name[:3] if re.search(r"[一-鿿]", t_name) else ""
    today     = datetime.date.today()

    def _wname2(r):
        return r.get("權證簡稱", r.get("權證名稱", ""))

    similar = [
        r for r in basic_all
        if r.get("權證代號", "").upper() != wc
        and (
            (und_code and _underlying_code(r) == und_code)
            or (und_short and len(und_short) >= 2 and und_short in _wname2(r))
        )
        and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        and "售" not in _wtype(r)   # 排除認售
    ]

    # 成交量篩選（同標的比較選項）
    MIN_VOL = 1000
    liquid_sim = [r for r in similar
                  if int(vol_map.get(r.get("權證代號",""), {}).get("成交張數", 0) or 0) >= MIN_VOL]
    similar_filtered = liquid_sim if liquid_sim else similar   # 無流動則退回所有

    print(f"🔍 {wc}: und_short={und_short!r}, 同標的候選={len(similar)}, 有成交量={len(liquid_sim)}", flush=True)
    ranked_sim = sorted(similar_filtered, key=lambda r: _score(r, vol_map), reverse=True)[:3]

    if ranked_sim:
        sim_lines = ["**同標的推薦認購權證**\n"]
        for i, row in enumerate(ranked_sim, 1):
            sim_lines.append(_fmt_warrant(row, vol_map, rank=i))
            sim_lines.append("")
        similar_summary = "\n".join(sim_lines)
    else:
        similar_summary = "（無其他符合條件的同標的認購權證）"

    return target_summary, similar_summary
