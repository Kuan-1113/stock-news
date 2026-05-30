"""
watchlist_db.py — 個人自選股 SQLite 存儲

功能：
  - 每個 Discord 用戶可在每個 Server 維護自己的自選股清單（最多 20 支）
  - 用戶可透過指令新增 / 刪除 / 查看清單
  - 資料庫路徑預設：watchlist.db（可由 WATCHLIST_DB_PATH 環境變數覆寫）

Railway 注意事項：
  - 預設 DB 存放在執行目錄，重新 Deploy 後資料會重設
  - 如需持久化，請掛載 Railway Volume 並設定：
    WATCHLIST_DB_PATH=/mnt/volume/watchlist.db
"""

import os
import sqlite3
import threading
import datetime

DB_PATH = os.environ.get(
    "WATCHLIST_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.db"),
)

_lock = threading.Lock()

MAX_STOCKS = 20   # 每位用戶最多自選股數量


# ── 連線 / 初始化 ─────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db() -> None:
    """建立資料表（若已存在則跳過），安全冪等。"""
    with _lock:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_watchlist (
                    user_id    TEXT NOT NULL,
                    guild_id   TEXT NOT NULL,
                    symbol     TEXT NOT NULL,
                    name       TEXT NOT NULL DEFAULT '',
                    added_at   TEXT NOT NULL,
                    PRIMARY KEY (user_id, guild_id, symbol)
                )
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_wl_user
                ON user_watchlist(user_id, guild_id)
            """)


# ── CRUD ──────────────────────────────────────────────────────────

def add_stock(
    user_id: str, guild_id: str, symbol: str, name: str = ""
) -> tuple[bool, str]:
    """
    新增一支股票到用戶自選股清單。
    回傳：(成功, 提示訊息)
    """
    symbol = symbol.upper().strip()
    with _lock:
        with _conn() as c:
            # 已存在？
            if c.execute(
                "SELECT 1 FROM user_watchlist WHERE user_id=? AND guild_id=? AND symbol=?",
                (user_id, guild_id, symbol),
            ).fetchone():
                return False, f"⚠️ `{symbol}` 已在你的自選股清單中。"

            # 超過上限？
            count = c.execute(
                "SELECT COUNT(*) FROM user_watchlist WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            ).fetchone()[0]
            if count >= MAX_STOCKS:
                return False, f"⚠️ 已達上限 {MAX_STOCKS} 支，請先刪除部分股票再新增。"

            now = datetime.datetime.now().isoformat(timespec="seconds")
            c.execute(
                "INSERT INTO user_watchlist VALUES (?, ?, ?, ?, ?)",
                (user_id, guild_id, symbol, name.strip(), now),
            )
            label = f"（{name}）" if name.strip() else ""
            return True, f"✅ 已將 `{symbol}`{label} 加入你的自選股清單！"


def remove_stock(
    user_id: str, guild_id: str, symbol: str
) -> tuple[bool, str]:
    """
    從用戶自選股清單移除一支股票。
    回傳：(成功, 提示訊息)
    """
    symbol = symbol.upper().strip()
    with _lock:
        with _conn() as c:
            r = c.execute(
                "DELETE FROM user_watchlist WHERE user_id=? AND guild_id=? AND symbol=?",
                (user_id, guild_id, symbol),
            )
            if r.rowcount:
                return True, f"✅ 已從自選股清單移除 `{symbol}`。"
            return False, f"❌ 找不到 `{symbol}`，請用 `/自選股清單` 確認代碼。"


def get_watchlist(user_id: str, guild_id: str) -> list[dict]:
    """取得用戶完整自選股清單，按加入時間排序。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, name, added_at FROM user_watchlist "
            "WHERE user_id=? AND guild_id=? ORDER BY added_at ASC",
            (user_id, guild_id),
        ).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"], "added_at": r["added_at"]} for r in rows]


def clear_watchlist(user_id: str, guild_id: str) -> int:
    """清空用戶所有自選股，回傳刪除筆數。"""
    with _lock:
        with _conn() as c:
            r = c.execute(
                "DELETE FROM user_watchlist WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            )
            return r.rowcount
