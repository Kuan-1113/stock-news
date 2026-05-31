"""
strategy_agent/chip_data.py — 三大法人籌碼資料模組

資料來源：TWSE OpenAPI（免費，盤後約 16:30 更新）
儲存位置：signal_db 同一個 SQLite（chip_daily 資料表）
歷史保留：30 天（足夠計算連買天數）

每日流程：
  1. init_chip_db()     → 建立資料表（冪等）
  2. update_chip_data() → 抓取今日 TWSE 資料並存入
  3. get_chip_map()     → 取出所有股票近 N 天籌碼歷史

環境變數：
  SIGNAL_DB_PATH — 同 signal_db.py
"""

from __future__ import annotations

import datetime
import requests

from strategy_agent.signal_db import _conn, _lock

# ── 設定 ─────────────────────────────────────────────────────────

# 主要：TWSE OpenAPI（只含當日最新，不支援 date 參數）
TWSE_API_OPENAPI = "https://openapi.twse.com.tw/v1/fund/TWT38U"
# 備用：傳統 TWSE（支援 date= 參數）
TWSE_API_LEGACY  = "https://www.twse.com.tw/fund/TWT38U"

CHIP_KEEP_DAYS   = 30   # 保留天數


# ── 資料庫 ────────────────────────────────────────────────────────

def init_chip_db() -> None:
    """建立 chip_daily 資料表（冪等）"""
    with _lock:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS chip_daily (
                    date        TEXT    NOT NULL,
                    symbol      TEXT    NOT NULL,
                    foreign_net INTEGER DEFAULT 0,   -- 外資（+外資自營商）淨買超
                    trust_net   INTEGER DEFAULT 0,   -- 投信淨買超
                    dealer_net  INTEGER DEFAULT 0,   -- 自營商淨買超
                    total_net   INTEGER DEFAULT 0,   -- 三大法人合計
                    PRIMARY KEY (date, symbol)
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_chip_sym
                ON chip_daily(symbol, date DESC)
            """)


# ── 解析工具 ──────────────────────────────────────────────────────

def _to_int(val) -> int:
    """解析含逗號的整數字串，錯誤回傳 0"""
    try:
        return int(str(val).replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        return 0


def _parse_openapi_record(item: dict) -> dict | None:
    """
    解析 TWSE OpenAPI TWT38U 單筆記錄。
    TWSE 英文 key 名可能因版本略有不同，使用多層 fallback。
    單位：千股（千股 / 1 = 張，因 1張=1千股）
    """
    code = str(item.get("Code", item.get("code", ""))).strip()
    if not code or not code.isdigit():
        return None

    symbol = f"{code}.TW"

    # 外資淨買超（外陸資 + 外資自營商）
    foreign_net = _to_int(
        item.get("ForeignInvestmentNetBuyOrSellShares")
        or item.get("ForeignInvestorsNetBuyShares")
        or item.get("ForeignNetBuyShares")
    )
    # 若 net 欄不存在，用 buy - sell 計算
    if foreign_net == 0 and (
        item.get("ForeignInvestmentBuyShares") or item.get("ForeignInvestorsBuyShares")
    ):
        f_buy  = _to_int(item.get("ForeignInvestmentBuyShares")
                         or item.get("ForeignInvestorsBuyShares", 0))
        f_sell = _to_int(item.get("ForeignInvestmentSellShares")
                         or item.get("ForeignInvestorsSellShares", 0))
        foreign_net = f_buy - f_sell

    # 投信淨買超
    trust_net = _to_int(
        item.get("InvestmentTrustNetBuyOrSellShares")
        or item.get("InvestmentTrustNetBuyShares")
    )
    if trust_net == 0 and item.get("InvestmentTrustBuyShares"):
        trust_net = (
            _to_int(item.get("InvestmentTrustBuyShares", 0))
            - _to_int(item.get("InvestmentTrustSellShares", 0))
        )

    # 自營商淨買超
    dealer_net = _to_int(
        item.get("DealerNetBuyOrSellShares")
        or item.get("DealerNetBuyShares")
    )
    if dealer_net == 0 and item.get("DealerBuyShares"):
        dealer_net = (
            _to_int(item.get("DealerBuyShares", 0))
            - _to_int(item.get("DealerSellShares", 0))
        )

    total_net = _to_int(
        item.get("TotalNetBuyOrSellShares")
        or item.get("TotalNetBuyShares")
    ) or (foreign_net + trust_net + dealer_net)

    return {
        "symbol":      symbol,
        "foreign_net": foreign_net,
        "trust_net":   trust_net,
        "dealer_net":  dealer_net,
        "total_net":   total_net,
    }


def _parse_legacy_record(fields: list[str], row: list) -> dict | None:
    """
    解析傳統 TWSE API（回傳格式：{"fields":[...], "data":[[...],...]}}）。
    傳統 API 欄位名稱為中文。
    """
    if not fields or not row or len(row) < len(fields):
        return None
    item = dict(zip(fields, row))

    # 代號欄位
    code = str(item.get("證券代號", item.get("代號", ""))).strip()
    if not code or not code.isdigit():
        return None
    symbol = f"{code}.TW"

    foreign_net = _to_int(item.get("外資及陸資(不含外資自營商)買賣超股數")
                          or item.get("外資買賣超股數")
                          or item.get("外陸資買賣超股數"))
    trust_net   = _to_int(item.get("投信買賣超股數"))
    dealer_net  = _to_int(item.get("自營商買賣超股數"))
    total_net   = _to_int(item.get("三大法人買賣超股數")) or (foreign_net + trust_net + dealer_net)

    return {
        "symbol":      symbol,
        "foreign_net": foreign_net,
        "trust_net":   trust_net,
        "dealer_net":  dealer_net,
        "total_net":   total_net,
    }


# ── 抓取 ─────────────────────────────────────────────────────────

def fetch_chip_from_twse(date_str: str) -> list[dict]:
    """
    抓取 TWSE 三大法人籌碼資料。
    先試 OpenAPI，失敗則試傳統 API（支援 date 參數）。
    """
    headers = {"User-Agent": "Mozilla/5.0"}

    # ── 嘗試 OpenAPI ───────────────────────────────────────────────
    try:
        r = requests.get(TWSE_API_OPENAPI, headers=headers, timeout=15)
        if r.status_code == 200:
            raw = r.json()
            if isinstance(raw, list) and len(raw) > 10:
                records = [_parse_openapi_record(x) for x in raw]
                records = [x for x in records if x]
                if records:
                    print(f"  📊 籌碼資料（OpenAPI）：{len(records)} 支")
                    return records
    except Exception as e:
        print(f"  ⚠️  OpenAPI 失敗：{e}")

    # ── 備用：傳統 TWSE API ────────────────────────────────────────
    try:
        d_str = date_str.replace("-", "")
        r = requests.get(
            TWSE_API_LEGACY,
            params={"response": "json", "date": d_str},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 200:
            data   = r.json()
            stat   = data.get("stat", "")
            if stat != "OK":
                print(f"  ⚠️  傳統 API stat={stat}（可能非交易日）")
                return []
            fields = data.get("fields", [])
            rows   = data.get("data",   [])
            records = [_parse_legacy_record(fields, row) for row in rows]
            records = [x for x in records if x]
            if records:
                print(f"  📊 籌碼資料（傳統 API）：{len(records)} 支")
                return records
    except Exception as e:
        print(f"  ⚠️  傳統 API 失敗：{e}")

    print("  ⚠️  無法取得 TWSE 籌碼資料（可能非交易日或 API 暫時異常）")
    return []


# ── 存儲 ─────────────────────────────────────────────────────────

def _store_chip_data(date_str: str, records: list[dict]) -> int:
    """批量寫入 chip_daily（冪等，INSERT OR REPLACE）"""
    if not records:
        return 0
    with _lock:
        with _conn() as c:
            c.executemany("""
                INSERT OR REPLACE INTO chip_daily
                    (date, symbol, foreign_net, trust_net, dealer_net, total_net)
                VALUES (?, ?, ?, ?, ?, ?)
            """, [
                (date_str, r["symbol"], r["foreign_net"],
                 r["trust_net"], r["dealer_net"], r["total_net"])
                for r in records
            ])
    return len(records)


def _cleanup_old_chip_data() -> None:
    """清除超過 CHIP_KEEP_DAYS 的舊資料"""
    cutoff = (datetime.date.today() - datetime.timedelta(days=CHIP_KEEP_DAYS)).isoformat()
    with _lock:
        with _conn() as c:
            c.execute("DELETE FROM chip_daily WHERE date < ?", (cutoff,))


def update_chip_data(date_str: str) -> int:
    """
    主要入口：抓取 + 儲存 + 清理。已有資料則跳過（冪等）。
    回傳：儲存筆數（0 = 今日非交易日或已存在）
    """
    with _conn() as c:
        cnt = c.execute(
            "SELECT COUNT(*) FROM chip_daily WHERE date=?", (date_str,)
        ).fetchone()[0]

    if cnt > 50:   # 上市股票 1000+ 支，50 筆以上視為已完整
        print(f"  📊 籌碼資料已存在（{cnt} 筆），跳過")
        return cnt

    records = fetch_chip_from_twse(date_str)
    stored  = _store_chip_data(date_str, records)
    if stored:
        _cleanup_old_chip_data()
    return stored


# ── 查詢 ─────────────────────────────────────────────────────────

def get_chip_history(symbol: str, days: int = 5) -> list[dict]:
    """取得某股票最近 N 天的籌碼記錄（由舊到新）"""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days + 7)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT date, foreign_net, trust_net, dealer_net, total_net
            FROM chip_daily
            WHERE symbol = ? AND date >= ?
            ORDER BY date ASC
            LIMIT ?
        """, (symbol, cutoff, days)).fetchall()
    return [dict(r) for r in rows]


def get_chip_map(days: int = 5) -> dict[str, list[dict]]:
    """
    取得所有股票最近 N 天籌碼歷史（批次查詢，避免 N+1）。
    回傳：{symbol: [record_oldest, ..., record_newest]}
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days + 7)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT symbol, date, foreign_net, trust_net, dealer_net, total_net
            FROM chip_daily
            WHERE date >= ?
            ORDER BY symbol ASC, date ASC
        """, (cutoff,)).fetchall()

    result: dict[str, list[dict]] = {}
    for row in rows:
        sym = row["symbol"]
        result.setdefault(sym, []).append({
            "date":        row["date"],
            "foreign_net": row["foreign_net"],
            "trust_net":   row["trust_net"],
            "dealer_net":  row["dealer_net"],
            "total_net":   row["total_net"],
        })
    return result
