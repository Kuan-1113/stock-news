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
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 快取（附 Lock 避免同時下載）──────────────────────────────────
_basic_cache: dict   = {"data": None, "ts": 0}
_daily_cache: dict   = {"data": None, "ts": 0}
_basic_lock          = threading.Lock()
_daily_lock          = threading.Lock()


def _fetch_basic_all() -> list:
    """下載並快取 t187ap37_L（全部權證基本資料）"""
    now = time.time()
    if _basic_cache["data"] is not None and (now - _basic_cache["ts"]) < 3600:
        return _basic_cache["data"]
    with _basic_lock:
        # 再次確認（另一個 thread 可能已下載完）
        if _basic_cache["data"] is not None and (time.time() - _basic_cache["ts"]) < 3600:
            return _basic_cache["data"]
        try:
            r = requests.get(
                f"{TWSE_OPENAPI}/t187ap37_L",
                headers=HEADERS, timeout=120,
            )
            if r.status_code == 200:
                data = r.json()
                _basic_cache["data"] = data
                _basic_cache["ts"]   = time.time()
                # 第一次載入時印出欄位名稱（用於除錯）
                if data:
                    print(f"✅ 權證基本資料已載入 {len(data)} 筆", flush=True)
                    print(f"🔍 欄位：{list(data[0].keys())[:15]}", flush=True)
                return _basic_cache["data"]
        except Exception as e:
            print(f"⚠️  t187ap37_L 下載失敗：{e}", flush=True)
    return _basic_cache["data"] or []


def _fetch_daily() -> list:
    """下載並快取 t187ap42_L（當日成交資料）"""
    now = time.time()
    if _daily_cache["data"] is not None and (now - _daily_cache["ts"]) < 1800:
        return _daily_cache["data"]
    with _daily_lock:
        if _daily_cache["data"] is not None and (time.time() - _daily_cache["ts"]) < 1800:
            return _daily_cache["data"]
        try:
            r = requests.get(
                f"{TWSE_OPENAPI}/t187ap42_L",
                headers=HEADERS, timeout=30,
            )
            if r.status_code == 200:
                _daily_cache["data"] = r.json()
                _daily_cache["ts"]   = time.time()
                return _daily_cache["data"]
        except Exception as e:
            print(f"⚠️  t187ap42_L 下載失敗：{e}", flush=True)
    return _daily_cache["data"] or []


def prewarm_cache():
    """Bot 啟動時在背景預先下載，確保第一個使用者不需等待"""
    def _task():
        print("🔄 預熱權證快取中…", flush=True)
        _fetch_basic_all()
        _fetch_daily()
        print("✅ 權證快取預熱完成", flush=True)
    threading.Thread(target=_task, daemon=True).start()


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
    return _field(row,
        "標的有價證券代號", "標的股票代號", "標的代號",
        "標的有価証券代號", "標的",
    )


def _expiry(row: dict) -> str:
    return _field(row, "到期日期", "到期日", "下市日期", "最後交易日", "期限")


def _strike(row: dict) -> str:
    return _field(row, "履約價格", "行使價格", "履約價", "執行價格")


def _ratio(row: dict) -> str:
    return _field(row, "行使比例", "換股比例", "權利比例", "行使率")


def _delta(row: dict) -> str:
    return _field(row, "Delta", "delta", "DELTA")


def _iv(row: dict) -> str:
    return _field(row, "隱含波動率", "IV", "隱波率", "波動率")


def _premium(row: dict) -> str:
    return _field(row, "溢價率", "溢價", "Premium")


def _leverage(row: dict) -> str:
    return _field(row, "實際槓桿倍數", "實際槓桿", "有效槓桿", "槓桿比例", "槓桿倍數")


def _prev_close(row: dict) -> str:
    return _field(row, "前一日收盤價", "昨收", "昨收盤", "收盤價", "前收")


def _wtype(row: dict) -> str:
    t = _field(row, "行使類型", "認購認售", "類型", "Type", "權證類型")
    if not t:
        name = row.get("權證名稱", "")
        return "認購" if "購" in name else ("認售" if "售" in name else "未知")
    return t


# ── 評分 ──────────────────────────────────────────────────────

def _score(row: dict, vol_map: dict) -> float:
    score = 0.0
    code  = row.get("權證代號", "")

    # 剩餘天數
    exp  = _expiry(row)
    days = _days_to_expiry(exp) if exp else 0
    if days <= 0:
        return -999.0
    if   30 <= days <= 120: score += 30.0
    elif days > 120:        score += 20.0
    elif 15 <= days < 30:   score += 10.0

    # 成交張數（流動性）
    vol = int(vol_map.get(code, {}).get("成交張數", 0) or 0)
    if   vol > 1_000_000: score += 30.0
    elif vol > 500_000:   score += 20.0
    elif vol > 100_000:   score += 10.0
    elif vol > 0:         score +=  2.0

    # 溢價率（低為佳）
    try:
        prem = float(_premium(row).replace("%", ""))
        if   prem < 3:   score += 20.0
        elif prem < 6:   score += 12.0
        elif prem < 10:  score +=  5.0
        elif prem >= 15: score -=  5.0
    except Exception:
        pass

    # Delta（認購 0.3~0.6 最佳）
    try:
        delta = abs(float(_delta(row)))
        if   0.3 <= delta <= 0.6:          score += 15.0
        elif 0.2 <= delta < 0.3 or 0.6 < delta <= 0.75: score += 8.0
    except Exception:
        pass

    # 實際槓桿（5~15 倍最佳）
    try:
        lev = float(_leverage(row))
        if   5 <= lev <= 15:             score += 15.0
        elif 3 <= lev < 5 or 15 < lev <= 25: score +=  7.0
    except Exception:
        pass

    return score


# ── 格式化單筆 ────────────────────────────────────────────────

def _fmt_warrant(row: dict, vol_map: dict, rank: int | None = None) -> str:
    code   = row.get("權證代號", "?")
    name   = row.get("權證名稱", "?")
    wt     = _wtype(row)
    exp    = _expiry(row)
    days   = _days_to_expiry(exp) if exp else "?"
    strike = _strike(row)
    ratio  = _ratio(row)
    delta  = _delta(row)
    iv     = _iv(row)
    prem   = _premium(row)
    lev    = _leverage(row)
    close  = _prev_close(row)
    vol    = vol_map.get(code, {}).get("成交張數", "N/A")
    prefix = f"#{rank} " if rank else ""
    return (
        f"{prefix}**{name}**（{code}） — {wt}\n"
        f"  昨收 {close} | 到期 {exp}（剩{days}天） | 履約價 {strike} | 行使比例 {ratio}\n"
        f"  Delta {delta} | IV {iv}% | 溢價率 {prem}% | 實際槓桿 {lev}x\n"
        f"  今日成交張數 {vol}"
    )


# ── 搜尋特定股票的好權證 ─────────────────────────────────────

def search_warrants(stock_input: str, top_n: int = 5) -> str:
    code, zh_name = lookup_stock(stock_input)

    basic_all = _fetch_basic_all()
    if not basic_all:
        return "❌ 無法取得權證基本資料，請稍後再試"

    today = datetime.date.today()

    # 方式1：用標的代號過濾
    matched = [
        r for r in basic_all
        if _underlying_code(r) == code
        and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
    ]

    # 方式2：用中文名稱過濾（前3字）
    if not matched and re.search(r"[一-鿿]", zh_name):
        short = zh_name[:3]
        matched = [
            r for r in basic_all
            if short in r.get("權證名稱", "")
            and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        ]

    # 方式3：從 t187ap42_L 裡用名稱找代號，再查 t187ap37_L
    if not matched and re.search(r"[一-鿿]", zh_name):
        short = zh_name[:3]
        daily = _fetch_daily()
        daily_codes = {
            r["權證代號"] for r in daily if short in r.get("權證名稱", "")
        }
        matched = [
            r for r in basic_all
            if r.get("權證代號", "") in daily_codes
            and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        ]

    if not matched:
        return f"❌ 找不到 {zh_name}（{code}）的有效認購權證（共查 {len(basic_all)} 筆）"

    # 只取認購
    calls = [r for r in matched if "購" in _wtype(r) or "購" in r.get("權證名稱", "")]
    if not calls:
        calls = matched  # 若全部都是認售，就全取

    daily     = _fetch_daily()
    vol_map   = {r.get("權證代號", ""): r for r in daily}
    ranked    = sorted(calls, key=lambda r: _score(r, vol_map), reverse=True)[:top_n]

    lines = [f"**{zh_name}（{code}）認購權證前{len(ranked)}名**\n"]
    for i, row in enumerate(ranked, 1):
        lines.append(_fmt_warrant(row, vol_map, rank=i))
        lines.append("")
    return "\n".join(lines)


# ── 分析指定權證 ─────────────────────────────────────────────

def analyze_warrant(warrant_code: str) -> tuple[str, str]:
    wc        = warrant_code.strip().upper()
    basic_all = _fetch_basic_all()
    daily     = _fetch_daily()

    target = next(
        (r for r in basic_all if r.get("權證代號", "").upper() == wc), None
    )
    if not target:
        return f"❌ 找不到權證代號 `{wc}`（已查詢 {len(basic_all)} 筆）", ""

    vol_map        = {r.get("權證代號", ""): r for r in daily}
    target_summary = _fmt_warrant(target, vol_map)

    und_code = _underlying_code(target)
    und_name = target.get("權證名稱", "")[:3]  # 從名稱推斷標的
    today    = datetime.date.today()

    similar = [
        r for r in basic_all
        if r.get("權證代號", "").upper() != wc
        and (
            (_underlying_code(r) == und_code and und_code)
            or (und_name and und_name in r.get("權證名稱", ""))
        )
        and _parse_date(_expiry(r)) and _parse_date(_expiry(r)) > today
        and ("購" in _wtype(r) or "購" in r.get("權證名稱", ""))
    ]

    ranked_sim = sorted(similar, key=lambda r: _score(r, vol_map), reverse=True)[:3]

    if ranked_sim:
        sim_lines = ["**同標的推薦認購權證**\n"]
        for i, row in enumerate(ranked_sim, 1):
            sim_lines.append(_fmt_warrant(row, vol_map, rank=i))
            sim_lines.append("")
        similar_summary = "\n".join(sim_lines)
    else:
        similar_summary = "（無其他符合條件的同標的認購權證）"

    return target_summary, similar_summary
