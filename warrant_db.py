"""
warrant_db.py — SQLite 權證持久化資料庫

表格：
  warrant_basic   基本資料（t187ap37_L，24h 刷新）
  warrant_daily   每日成交（t187ap42_L，當日刷新）
  price_history   歷史價格快照（每天記錄一次，用於估算 Delta）
  db_meta         時間戳記等 key-value

Railway 部署說明：
  - 預設路徑 warrants.db 在容器內，重新 Deploy 後清空（可接受）
  - 如需跨 Deploy 持久化，請掛載 Railway Volume 並設定環境變數：
    WARRANT_DB_PATH=/mnt/volume/warrants.db
"""

import os
import re
import json
import sqlite3
import threading
import datetime
from typing import Optional

DB_PATH = os.environ.get(
    "WARRANT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "warrants.db"),
)

_lock = threading.Lock()


# ── 連線 / 初始化 ─────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    """建立資料表（冪等，可重複呼叫）"""
    with _lock:
        with _conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS warrant_basic (
                    code        TEXT PRIMARY KEY,
                    name        TEXT,
                    wtype       TEXT,
                    und_code    TEXT,
                    expiry      TEXT,
                    status      TEXT DEFAULT 'active',
                    is_anomaly  INTEGER DEFAULT 0,
                    raw         TEXT NOT NULL,
                    updated_at  TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_basic_und
                    ON warrant_basic(und_code);
                CREATE INDEX IF NOT EXISTS idx_basic_exp
                    ON warrant_basic(expiry);
                CREATE INDEX IF NOT EXISTS idx_basic_status
                    ON warrant_basic(status);

                CREATE TABLE IF NOT EXISTS warrant_daily (
                    code        TEXT,
                    trade_date  TEXT,
                    raw         TEXT NOT NULL,
                    PRIMARY KEY (code, trade_date)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_code
                    ON warrant_daily(code);

                CREATE TABLE IF NOT EXISTS price_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    code        TEXT NOT NULL,
                    und_code    TEXT,
                    recorded_at TEXT NOT NULL,
                    w_price     REAL,
                    s_price     REAL,
                    ratio_f     REAL,
                    iv_bs       REAL,
                    delta_bs    REAL,
                    premium_pct REAL
                );
                CREATE INDEX IF NOT EXISTS idx_ph_code
                    ON price_history(code, recorded_at DESC);

                CREATE TABLE IF NOT EXISTS bs_cache (
                    code        TEXT PRIMARY KEY,
                    cached_at   TEXT,
                    iv          REAL,
                    delta       REAL,
                    theta       REAL,
                    gamma       REAL,
                    leverage    REAL,
                    premium_pct REAL,
                    anomaly     TEXT
                );

                CREATE TABLE IF NOT EXISTS db_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            # 遷移：舊表缺少欄位時補上（相容既有 DB）
            for migration in [
                "ALTER TABLE warrant_basic ADD COLUMN status TEXT DEFAULT 'active'",
                "ALTER TABLE warrant_basic ADD COLUMN is_anomaly INTEGER DEFAULT 0",
                "ALTER TABLE price_history ADD COLUMN iv_bs REAL",
                "ALTER TABLE price_history ADD COLUMN delta_bs REAL",
                "ALTER TABLE price_history ADD COLUMN premium_pct REAL",
            ]:
                try:
                    c.execute(migration)
                except Exception:
                    pass   # 欄位已存在
    print(f"✅ 權證資料庫就緒：{DB_PATH}", flush=True)


# ── helpers ──────────────────────────────────────────────────────

def _set_meta(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES (?,?)", (key, value)
    )


def get_meta(key: str) -> Optional[str]:
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM db_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None


def _meta_age_hours(key: str) -> float:
    """回傳該 meta 距今幾小時（不存在→99999）"""
    ts = get_meta(key)
    if not ts:
        return 99999.0
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return (datetime.datetime.now() - dt).total_seconds() / 3600
    except Exception:
        return 99999.0


# ── 基本資料 ─────────────────────────────────────────────────────

def save_basic(rows: list) -> int:
    """
    將 t187ap37_L 整批寫入 DB。
    同時建立索引欄位（und_code / expiry）方便 SQL 查詢。
    """
    if not rows:
        return 0
    now = datetime.datetime.now().isoformat()
    records = []
    for r in rows:
        code = r.get("權證代號", "").strip()
        if not code:
            continue
        und_raw = r.get("標的證券/指數", "")
        m = re.search(r"\b(\d{4})\b", und_raw)
        und_code = m.group(1) if m else und_raw
        records.append((
            code,
            r.get("權證簡稱", r.get("權證名稱", "")),
            r.get("權證類型", ""),
            und_code,
            r.get("最後交易日", ""),
            json.dumps(r, ensure_ascii=False),
            now,
        ))
    if not records:
        return 0
    with _lock:
        with _conn() as c:
            c.executemany("""
                INSERT OR REPLACE INTO warrant_basic
                    (code, name, wtype, und_code, expiry, raw, updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, records)
            _set_meta(c, "basic_updated_at", now)
            _set_meta(c, "basic_count", str(len(records)))
    print(f"✅ 基本資料入庫：{len(records)} 筆", flush=True)
    return len(records)


def load_basic_all() -> list:
    """從 DB 載入全部基本資料（格式與 t187ap37_L 相容）"""
    with _conn() as c:
        rows = c.execute("SELECT raw FROM warrant_basic").fetchall()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row["raw"]))
        except Exception:
            pass
    return result


def basic_age_hours() -> float:
    return _meta_age_hours("basic_updated_at")


def basic_count() -> int:
    try:
        return int(get_meta("basic_count") or "0")
    except Exception:
        return 0


# ── 每日成交 ─────────────────────────────────────────────────────

def save_daily(rows: list) -> int:
    """將 t187ap42_L 整批寫入 DB（當日資料）"""
    if not rows:
        return 0
    today = datetime.date.today().isoformat()
    now   = datetime.datetime.now().isoformat()
    records = []
    for r in rows:
        code = r.get("權證代號", r.get("代號", "")).strip()
        if not code:
            continue
        records.append((code, today, json.dumps(r, ensure_ascii=False)))
    if not records:
        return 0
    with _lock:
        with _conn() as c:
            c.executemany("""
                INSERT OR REPLACE INTO warrant_daily (code, trade_date, raw)
                VALUES (?,?,?)
            """, records)
            _set_meta(c, "daily_updated_at", now)
    return len(records)


def load_daily_today() -> list:
    """載入今日成交資料"""
    today = datetime.date.today().isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT raw FROM warrant_daily WHERE trade_date=?", (today,)
        ).fetchall()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row["raw"]))
        except Exception:
            pass
    return result


def daily_age_hours() -> float:
    return _meta_age_hours("daily_updated_at")


# ── 歷史價格快照 ──────────────────────────────────────────────────

def record_price(code: str, und_code: str,
                 w_price: float, s_price: float, ratio_f: float,
                 iv_bs: float = None, delta_bs: float = None,
                 premium_pct: float = None):
    """
    記錄一次價格快照（同一天已有記錄則跳過）。
    包含 BS 計算結果（IV / Delta / 溢價率）方便長期趨勢分析。
    """
    if w_price <= 0:
        return
    today = datetime.date.today().isoformat()
    with _lock:
        with _conn() as c:
            exist = c.execute(
                "SELECT id FROM price_history WHERE code=? AND recorded_at LIKE ?",
                (code, today + "%"),
            ).fetchone()
            if exist:
                return
            c.execute("""
                INSERT INTO price_history
                    (code, und_code, recorded_at, w_price, s_price, ratio_f,
                     iv_bs, delta_bs, premium_pct)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                code, und_code,
                datetime.datetime.now().isoformat(),
                w_price,
                s_price if s_price > 0 else None,
                ratio_f if ratio_f > 0 else None,
                iv_bs, delta_bs, premium_pct,
            ))


def calc_delta(code: str) -> Optional[float]:
    """
    從歷史資料估算 Delta（需 ≥5 天有效差分）。
    公式：Delta ≈ 平均( ΔW / ratio_f / ΔS )
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT w_price, s_price, ratio_f FROM price_history
            WHERE code=?
              AND w_price IS NOT NULL
              AND s_price IS NOT NULL
              AND ratio_f IS NOT NULL
            ORDER BY recorded_at DESC LIMIT 30
        """, (code,)).fetchall()

    if len(rows) < 5:
        return None

    deltas = []
    for i in range(len(rows) - 1):
        dw = rows[i]["w_price"] - rows[i + 1]["w_price"]
        ds = rows[i]["s_price"] - rows[i + 1]["s_price"]
        rf = rows[i]["ratio_f"]
        if abs(ds) < 0.5 or rf <= 0:
            continue
        d = (dw / rf) / ds
        if 0.01 <= d <= 1.0:
            deltas.append(d)

    if len(deltas) < 3:
        return None

    deltas.sort()
    trimmed = deltas[1:-1] if len(deltas) > 4 else deltas
    return round(sum(trimmed) / len(trimmed), 3)


def get_history_days(code: str) -> int:
    """回傳已累積幾天的歷史資料"""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT DATE(recorded_at)) AS n FROM price_history WHERE code=?",
            (code,),
        ).fetchone()
        return row["n"] if row else 0


def get_tracked_warrants(limit: int = 200) -> list[dict]:
    """回傳最近有記錄的權證清單（供排程批量更新）"""
    with _conn() as c:
        rows = c.execute("""
            SELECT code, und_code,
                   AVG(ratio_f) AS ratio_f,
                   MAX(recorded_at) AS last_seen
            FROM price_history
            GROUP BY code
            ORDER BY last_seen DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [
        {"code": r["code"], "und_code": r["und_code"] or "", "ratio_f": r["ratio_f"] or 1.0}
        for r in rows
    ]


# ── 生命週期狀態同步 ──────────────────────────────────────────────

def sync_warrant_status():
    """
    依據到期日將 warrant_basic 的 status 欄位更新：
    - expired     : expiry <= today
    - near_expiry : expiry <= today + 30 days
    - active      : otherwise
    此函式在每日排程時呼叫一次即可。
    """
    today   = datetime.date.today()
    near_dt = today + datetime.timedelta(days=30)
    today_s = today.isoformat().replace("-", "")
    near_s  = near_dt.isoformat().replace("-", "")

    with _lock:
        with _conn() as c:
            c.execute("""
                UPDATE warrant_basic SET status='expired'
                WHERE expiry != '' AND expiry <= ?
            """, (today_s,))
            c.execute("""
                UPDATE warrant_basic SET status='near_expiry'
                WHERE expiry > ? AND expiry <= ? AND status != 'expired'
            """, (today_s, near_s))
            c.execute("""
                UPDATE warrant_basic SET status='active'
                WHERE expiry > ? AND status NOT IN ('expired','near_expiry')
            """, (near_s,))
            expired_cnt = c.execute(
                "SELECT COUNT(*) FROM warrant_basic WHERE status='expired'"
            ).fetchone()[0]
            near_cnt = c.execute(
                "SELECT COUNT(*) FROM warrant_basic WHERE status='near_expiry'"
            ).fetchone()[0]
    print(f"✅ 狀態同步：已到期 {expired_cnt}，即將到期 {near_cnt}", flush=True)


# ── 異常標記 ──────────────────────────────────────────────────────

def mark_anomaly(code: str, is_anomaly: bool = True):
    """標記單一權證為異常（溢價過高/零成交等）"""
    with _lock:
        with _conn() as c:
            c.execute(
                "UPDATE warrant_basic SET is_anomaly=? WHERE code=?",
                (1 if is_anomaly else 0, code),
            )


def get_anomaly_codes() -> set[str]:
    """回傳所有已標記異常的代號集合"""
    with _conn() as c:
        rows = c.execute(
            "SELECT code FROM warrant_basic WHERE is_anomaly=1"
        ).fetchall()
    return {r["code"] for r in rows}


# ── BS 快取（避免重複計算）────────────────────────────────────────

def save_bs_cache(code: str, bs_result: dict):
    """儲存 Black-Scholes 計算結果（用於排行 / 比較）"""
    now = datetime.datetime.now().isoformat()
    with _lock:
        with _conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO bs_cache
                    (code, cached_at, iv, delta, theta, gamma, leverage, premium_pct, anomaly)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                code, now,
                bs_result.get("iv"),
                bs_result.get("delta"),
                bs_result.get("theta"),
                bs_result.get("gamma"),
                bs_result.get("leverage"),
                bs_result.get("premium_pct"),
                bs_result.get("anomaly", ""),
            ))


def get_bs_cache(code: str, max_age_hours: float = 6.0) -> Optional[dict]:
    """讀取快取的 BS 計算結果（超過 max_age_hours 則視為過期）"""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM bs_cache WHERE code=?", (code,)
        ).fetchone()
    if not row:
        return None
    try:
        cached_dt = datetime.datetime.fromisoformat(row["cached_at"])
        age = (datetime.datetime.now() - cached_dt).total_seconds() / 3600
        if age > max_age_hours:
            return None
    except Exception:
        return None
    return dict(row)


# ── IV 歷史趨勢 ────────────────────────────────────────────────────

def get_iv_history(code: str, days: int = 30) -> list[dict]:
    """
    回傳最近 N 天的 IV 歷史（供 IV Percentile 計算）
    """
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT DATE(recorded_at) AS dt, iv_bs, premium_pct
            FROM price_history
            WHERE code=? AND iv_bs IS NOT NULL AND recorded_at >= ?
            ORDER BY recorded_at
        """, (code, cutoff)).fetchall()
    return [{"date": r["dt"], "iv": r["iv_bs"], "premium": r["premium_pct"]} for r in rows]


def calc_iv_percentile(code: str) -> Optional[float]:
    """
    計算當前 IV 相對歷史的百分位（0-100），
    用於判斷現在 IV 是偏高還是偏低。
    """
    hist = get_iv_history(code, days=90)
    if len(hist) < 5:
        return None
    ivs = [h["iv"] for h in hist if h["iv"]]
    if not ivs:
        return None
    current_iv = ivs[-1]
    below = sum(1 for v in ivs if v <= current_iv)
    return round(below / len(ivs) * 100, 1)
