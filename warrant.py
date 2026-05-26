"""
warrant.py  —  台灣上市認購(售)權證查詢與分析
資料來源：TWSE OpenAPI
  t187ap37_L  上市權證基本資料彙總表（含 Delta/IV/溢價率/槓桿）
  t187ap42_L  上市認購(售)權證每日成交資料
"""

import re
import time
import datetime
import requests

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1/opendata"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── 快取（每小時更新一次）────────────────────────────────────────
_basic_cache: dict = {"data": None, "ts": 0}
_daily_cache: dict = {"data": None, "ts": 0}

def _fetch_basic_all() -> list:
    """下載並快取 t187ap37_L（全部權證基本資料，約 5-15 MB）"""
    now = time.time()
    if _basic_cache["data"] is not None and (now - _basic_cache["ts"]) < 3600:
        return _basic_cache["data"]
    try:
        r = requests.get(
            f"{TWSE_OPENAPI}/t187ap37_L",
            headers=HEADERS, timeout=90,
        )
        if r.status_code == 200:
            _basic_cache["data"] = r.json()
            _basic_cache["ts"] = now
            print(f"✅ 權證基本資料已載入 {len(_basic_cache['data'])} 筆", flush=True)
            return _basic_cache["data"]
    except Exception as e:
        print(f"⚠️  t187ap37_L 下載失敗：{e}", flush=True)
    return _basic_cache["data"] or []


def _fetch_daily() -> list:
    """下載並快取 t187ap42_L（當日成交資料）"""
    now = time.time()
    if _daily_cache["data"] is not None and (now - _daily_cache["ts"]) < 1800:
        return _daily_cache["data"]
    try:
        r = requests.get(
            f"{TWSE_OPENAPI}/t187ap42_L",
            headers=HEADERS, timeout=30,
        )
        if r.status_code == 200:
            _daily_cache["data"] = r.json()
            _daily_cache["ts"] = now
            return _daily_cache["data"]
    except Exception as e:
        print(f"⚠️  t187ap42_L 下載失敗：{e}", flush=True)
    return _daily_cache["data"] or []


# ── 股票代號查詢 ──────────────────────────────────────────────

def lookup_stock(query: str) -> tuple[str, str]:
    """
    輸入代號(2330)或中文名(台積電) → (code, zh_name)
    優先用 Yahoo Finance 搜尋
    """
    q = query.strip()
    # 純數字 4 碼 → 直接查 Yahoo 取中文名
    if re.match(r"^\d{4}$", q):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{q}.TW?interval=1d&range=1d",
                headers=HEADERS, timeout=8,
            )
            if r.status_code == 200:
                meta = r.json()["chart"]["result"][0]["meta"]
                name = meta.get("shortName", q)
                return q, name
        except Exception:
            pass
        return q, q

    # 中文名 → 用 Yahoo Finance 搜尋
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v1/finance/search?q={requests.utils.quote(q)}&newsCount=0&quotesCount=5&region=TW&lang=zh-TW",
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200:
            for item in r.json().get("quotes", []):
                sym = item.get("symbol", "")
                if re.match(r"^\d{4}\.TW$", sym):
                    code = sym.replace(".TW", "")
                    name = item.get("longname") or item.get("shortname", code)
                    return code, name
    except Exception:
        pass

    # 找不到 → 原樣返回
    return q, q


# ── 日期處理 ──────────────────────────────────────────────────

def _parse_date(s: str) -> datetime.date | None:
    """支援 1150815 (民國) 或 20250815 (西元) 兩種格式"""
    s = str(s).strip().replace("/", "").replace("-", "")
    try:
        if len(s) == 7:          # 民國年 yyyMMdd → 加 1911
            y = int(s[:3]) + 1911
            m, d = int(s[3:5]), int(s[5:7])
        elif len(s) == 8:        # 西元 yyyyMMdd
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


# ── 欄位自動偵測 ───────────────────────────────────────────────

def _field(row: dict, *candidates) -> str:
    for k in candidates:
        if k in row:
            return str(row[k]).strip()
    return ""


def _underlying_code(row: dict) -> str:
    return _field(row, "標的有價證券代號", "標的股票代號", "標的代號", "標的")


def _expiry(row: dict) -> str:
    return _field(row, "到期日期", "到期日", "下市日期", "最後交易日")


def _strike(row: dict) -> str:
    return _field(row, "履約價格", "行使價格", "履約價")


def _ratio(row: dict) -> str:
    return _field(row, "行使比例", "換股比例", "權利比例")


def _delta(row: dict) -> str:
    return _field(row, "Delta", "delta", "DELTA", "δ")


def _iv(row: dict) -> str:
    return _field(row, "隱含波動率", "IV", "隱波率")


def _premium(row: dict) -> str:
    return _field(row, "溢價率", "溢價", "Premium")


def _leverage(row: dict) -> str:
    return _field(row, "實際槓桿倍數", "實際槓桿", "有效槓桿", "槓桿比例")


def _prev_close(row: dict) -> str:
    return _field(row, "前一日收盤價", "昨收", "昨收盤", "收盤價")


def _wtype(row: dict) -> str:
    """認購 or 認售"""
    t = _field(row, "行使類型", "認購認售", "類型", "Type")
    if not t:
        name = row.get("權證名稱", "")
        if "購" in name:
            return "認購"
        if "售" in name:
            return "認售"
    return t or "未知"


# ── 評分函數 ──────────────────────────────────────────────────

def _score(row: dict, vol_map: dict) -> float:
    """
    綜合評分（越高越好）：
    - 剩餘天數 30-120 天最佳
    - 成交張數（流動性）
    - 溢價率低
    - Delta 適中（0.3~0.6 最佳）
    - 實際槓桿 5~15 倍最佳
    """
    score = 0.0
    code = row.get("權證代號", "")

    # 剩餘天數
    exp = _expiry(row)
    days = _days_to_expiry(exp) if exp else 0
    if days <= 0:
        return -999.0
    if 30 <= days <= 120:
        score += 30.0
    elif days > 120:
        score += 20.0
    elif 15 <= days < 30:
        score += 10.0

    # 成交張數（流動性）
    vol = int(vol_map.get(code, {}).get("成交張數", 0) or 0)
    if vol > 1_000_000:
        score += 30.0
    elif vol > 500_000:
        score += 20.0
    elif vol > 100_000:
        score += 10.0
    elif vol > 0:
        score += 2.0

    # 溢價率（越低越好）
    prem_s = _premium(row)
    try:
        prem = float(prem_s.replace("%", ""))
        if prem < 3:
            score += 20.0
        elif prem < 6:
            score += 12.0
        elif prem < 10:
            score += 5.0
        elif prem >= 15:
            score -= 5.0
    except Exception:
        pass

    # Delta（認購 0.3-0.6 最佳）
    delta_s = _delta(row)
    try:
        delta = abs(float(delta_s))
        if 0.3 <= delta <= 0.6:
            score += 15.0
        elif 0.2 <= delta < 0.3 or 0.6 < delta <= 0.75:
            score += 8.0
    except Exception:
        pass

    # 實際槓桿 5~15 倍最佳
    lev_s = _leverage(row)
    try:
        lev = float(lev_s)
        if 5 <= lev <= 15:
            score += 15.0
        elif 3 <= lev < 5 or 15 < lev <= 25:
            score += 7.0
    except Exception:
        pass

    return score


# ── 格式化單一權證摘要（給 Claude 用）──────────────────────────

def _fmt_warrant(row: dict, vol_map: dict, rank: int | None = None) -> str:
    code = row.get("權證代號", "?")
    name = row.get("權證名稱", "?")
    wt   = _wtype(row)
    exp  = _expiry(row)
    days = _days_to_expiry(exp) if exp else "?"
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


# ── 主功能：搜尋特定股票的好權證 ────────────────────────────────

def search_warrants(stock_input: str, top_n: int = 5) -> str:
    """
    輸入股票代號或名稱，回傳 top_n 優質認購權證摘要（給 Claude 分析用）
    """
    code, zh_name = lookup_stock(stock_input)

    basic_all = _fetch_basic_all()
    if not basic_all:
        return f"❌ 無法取得權證基本資料"

    # 過濾出此標的的認購權證（未到期）
    today = datetime.date.today()
    matched = []
    for row in basic_all:
        uc = _underlying_code(row)
        exp_str = _expiry(row)
        exp_date = _parse_date(exp_str) if exp_str else None
        if uc == code and exp_date and exp_date > today:
            matched.append(row)

    # 如果靠代碼沒找到，試試用名稱中有股票名稱
    if not matched and len(zh_name) >= 2:
        short_name = zh_name[:4]  # 取前4字避免太長
        for row in basic_all:
            name = row.get("權證名稱", "")
            exp_str = _expiry(row)
            exp_date = _parse_date(exp_str) if exp_str else None
            if short_name in name and exp_date and exp_date > today:
                matched.append(row)

    if not matched:
        return f"❌ 找不到 {zh_name}（{code}）的有效認購權證"

    # 建立成交張數對照表
    daily = _fetch_daily()
    vol_map: dict = {}
    for row in daily:
        wc = row.get("權證代號", "")
        if wc:
            vol_map[wc] = row

    # 評分排序，只取認購
    call_warrants = [r for r in matched if "購" in _wtype(r) or "購" in r.get("權證名稱", "")]
    ranked = sorted(call_warrants, key=lambda r: _score(r, vol_map), reverse=True)[:top_n]

    if not ranked:
        return f"❌ 找不到 {zh_name}（{code}）的合適認購權證"

    lines = [f"**{zh_name}（{code}）認購權證前{len(ranked)}名**\n"]
    for i, row in enumerate(ranked, 1):
        lines.append(_fmt_warrant(row, vol_map, rank=i))
        lines.append("")

    return "\n".join(lines)


# ── 主功能：分析指定權證 ─────────────────────────────────────

def analyze_warrant(warrant_code: str) -> tuple[str, str]:
    """
    輸入權證代號，回傳 (warrant_summary, similar_warrants_summary)
    兩者都是要給 Claude 分析的文字
    """
    wc = warrant_code.strip().upper()

    basic_all = _fetch_basic_all()
    daily = _fetch_daily()

    # 找此權證
    target = next((r for r in basic_all if r.get("權證代號", "").upper() == wc), None)
    if not target:
        return f"❌ 找不到權證代號 `{wc}`", ""

    # 當日成交
    vol_map = {r.get("權證代號", ""): r for r in daily}

    target_summary = _fmt_warrant(target, vol_map)

    # 找同標的其他認購權證
    und_code = _underlying_code(target)
    today = datetime.date.today()
    similar = []
    for row in basic_all:
        if (row.get("權證代號", "").upper() == wc):
            continue  # 排除自己
        rc = _underlying_code(row)
        exp_str = _expiry(row)
        exp_date = _parse_date(exp_str) if exp_str else None
        is_call = "購" in _wtype(row) or "購" in row.get("權證名稱", "")
        if rc == und_code and exp_date and exp_date > today and is_call:
            similar.append(row)

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
