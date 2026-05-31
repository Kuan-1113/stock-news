"""
strategy_agent/signal_db.py — 策略信號資料庫

Schema:
  signals：每日信號 + 5/10 日後勝敗追蹤
  strategy_stats：快取各策略近期勝率（加速查詢）

勝敗定義：
  10 日後漲幅 >= +3%  → win  (1)
  10 日後漲幅 <= -3%  → loss (0)
  介於 -3% ~ +3%      → neutral (NULL，不計入勝率)
"""

import os
import sqlite3
import threading
import datetime

DB_PATH = os.environ.get(
    "SIGNAL_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "strategy_signals.db"),
)

_lock = threading.Lock()

WIN_PCT  = 3.0   # 漲超過 3% → 算贏
LOSS_PCT = 3.0   # 跌超過 3% → 算輸


# ── 連線 ─────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db() -> None:
    """建立資料表（冪等），並自動 migration 加欄位"""
    with _lock:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    date             TEXT    NOT NULL,
                    symbol           TEXT    NOT NULL,
                    name             TEXT    NOT NULL DEFAULT '',
                    etf_source       TEXT    NOT NULL DEFAULT '',
                    strategy         TEXT    NOT NULL,
                    entry_price      REAL    NOT NULL,
                    price_5d         REAL,
                    price_10d        REAL,
                    result_5d        REAL,
                    result_10d       REAL,
                    win_5d           INTEGER,
                    win_10d          INTEGER,
                    sent_discord     INTEGER NOT NULL DEFAULT 0,
                    confidence_stars INTEGER NOT NULL DEFAULT 1,  -- ⭐1/⭐⭐2/⭐⭐⭐3
                    strategy_type    TEXT    NOT NULL DEFAULT 'technical', -- technical/chip
                    UNIQUE(date, symbol, strategy)
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_sig_date     ON signals(date DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sig_strategy ON signals(strategy, date DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sig_stars    ON signals(confidence_stars DESC)")

            # ── 既有 DB 自動 migration（加新欄位）──────────────────
            existing_cols = {row[1] for row in c.execute("PRAGMA table_info(signals)")}
            if "confidence_stars" not in existing_cols:
                c.execute("ALTER TABLE signals ADD COLUMN confidence_stars INTEGER NOT NULL DEFAULT 1")
                print("  ✅ migration: 加入 confidence_stars 欄位")
            if "strategy_type" not in existing_cols:
                c.execute("ALTER TABLE signals ADD COLUMN strategy_type TEXT NOT NULL DEFAULT 'technical'")
                print("  ✅ migration: 加入 strategy_type 欄位")

    print(f"  ✅ signal_db 初始化完成（{DB_PATH}）")


# ── 寫入信號 ──────────────────────────────────────────────────────

def add_signal(
    date: str, symbol: str, name: str,
    etf_source: str, strategy: str, entry_price: float,
    confidence_stars: int = 1,
    strategy_type: str = "technical",
) -> bool:
    """新增信號（IGNORE ON CONFLICT 確保冪等）"""
    with _lock:
        with _conn() as c:
            cur = c.execute("""
                INSERT OR IGNORE INTO signals
                  (date, symbol, name, etf_source, strategy, entry_price,
                   confidence_stars, strategy_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, symbol, name, etf_source, strategy,
                  round(entry_price, 2), confidence_stars, strategy_type))
            return cur.rowcount > 0


def update_confidence_stars(date: str, symbol: str, stars: int) -> None:
    """更新同一標的當日所有信號的信心星級"""
    with _lock:
        with _conn() as c:
            c.execute(
                "UPDATE signals SET confidence_stars=? WHERE date=? AND symbol=?",
                (stars, date, symbol),
            )


# ── 更新歷史結果 ───────────────────────────────────────────────────

def get_pending_signals(max_days: int = 15) -> list:
    """
    取得還沒更新 result 的歷史信號。
    只看 max_days 天內（太久以前就跳過）。
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=max_days)).isoformat()
    with _conn() as c:
        return c.execute("""
            SELECT id, date, symbol, entry_price, win_5d, win_10d
            FROM signals
            WHERE date >= ?
              AND (win_5d IS NULL OR win_10d IS NULL)
            ORDER BY date ASC
        """, (cutoff,)).fetchall()


def update_result(signal_id: int, days: int, current_price: float, entry_price: float) -> None:
    """填入 N 日後的實際報酬"""
    pct = (current_price - entry_price) / entry_price * 100
    win = None
    if pct >= WIN_PCT:
        win = 1
    elif pct <= -LOSS_PCT:
        win = 0
    # pct 介於 ±3% → win = NULL（中性，不計入勝率）

    with _lock:
        with _conn() as c:
            if days == 5:
                c.execute(
                    "UPDATE signals SET price_5d=?, result_5d=?, win_5d=? WHERE id=?",
                    (round(current_price, 2), round(pct, 2), win, signal_id),
                )
            elif days == 10:
                c.execute(
                    "UPDATE signals SET price_10d=?, result_10d=?, win_10d=? WHERE id=?",
                    (round(current_price, 2), round(pct, 2), win, signal_id),
                )


# ── 勝率查詢 ──────────────────────────────────────────────────────

def get_strategy_winrate(strategy: str, window: int = 60) -> tuple[float | None, int, float]:
    """
    計算策略近 window 個有效信號（win_10d IS NOT NULL）的勝率。
    回傳 (勝率%, 信號數, 平均報酬%)
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT win_10d, result_10d
            FROM signals
            WHERE strategy=? AND win_10d IS NOT NULL
            ORDER BY date DESC LIMIT ?
        """, (strategy, window)).fetchall()

    if not rows:
        return None, 0, 0.0

    wins   = sum(1 for r in rows if r["win_10d"] == 1)
    losses = sum(1 for r in rows if r["win_10d"] == 0)
    total  = wins + losses
    avg_r  = sum(r["result_10d"] for r in rows if r["result_10d"] is not None) / len(rows)

    winrate = round(wins / total * 100, 1) if total > 0 else None
    return winrate, len(rows), round(avg_r, 2)


def get_all_strategy_stats(strategies: list[str]) -> dict:
    """批次取得所有策略勝率統計"""
    return {
        s: get_strategy_winrate(s) for s in strategies
    }


# ── 今日信號查詢 ───────────────────────────────────────────────────

def get_today_signals(date: str | None = None) -> list:
    date = date or datetime.date.today().isoformat()
    with _conn() as c:
        return c.execute("""
            SELECT * FROM signals WHERE date=?
            ORDER BY strategy, symbol
        """, (date,)).fetchall()


def mark_sent(signal_ids: list[int]) -> None:
    with _lock:
        with _conn() as c:
            c.executemany(
                "UPDATE signals SET sent_discord=1 WHERE id=?",
                [(i,) for i in signal_ids],
            )


# ── 統計摘要 ──────────────────────────────────────────────────────

def get_summary_stats() -> dict:
    """取得整體統計（給 /明牌 指令顯示）"""
    with _conn() as c:
        total   = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM signals WHERE win_10d IS NULL").fetchone()[0]
        wins    = c.execute("SELECT COUNT(*) FROM signals WHERE win_10d=1").fetchone()[0]
        losses  = c.execute("SELECT COUNT(*) FROM signals WHERE win_10d=0").fetchone()[0]
    return {
        "total": total, "pending": pending,
        "wins": wins, "losses": losses,
        "winrate": round(wins/(wins+losses)*100, 1) if (wins+losses) > 0 else None,
    }
