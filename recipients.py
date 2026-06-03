"""接收者清單的 SQLite 持久化。

DB 檔預設在專案目錄底下的 recipients.db，
透過 Bot 指令動態增減，主程式啟動時若 DB 空且設了 BOT_TARGET 會自動 bootstrap 第一筆。
"""

import sqlite3
from datetime import datetime, timezone

DB_FILE = "recipients.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                chat_id  INTEGER PRIMARY KEY,
                name     TEXT,
                enabled  INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL
            )
            """
        )


def add(chat_id: int, name: str | None = None) -> bool:
    """新增接收者。chat_id 已存在則回 False，不覆寫 name。"""
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO recipients (chat_id, name, enabled, added_at) VALUES (?, ?, 1, ?)",
                (chat_id, name, datetime.now(timezone.utc).isoformat()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove(chat_id: int) -> bool:
    with _conn() as conn:
        return conn.execute("DELETE FROM recipients WHERE chat_id = ?", (chat_id,)).rowcount > 0


def set_enabled(chat_id: int, enabled: bool) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE recipients SET enabled = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id),
        )
        return cur.rowcount > 0


def list_all() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, name, enabled, added_at FROM recipients ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def list_enabled_ids() -> list[int]:
    with _conn() as conn:
        return [
            r[0]
            for r in conn.execute("SELECT chat_id FROM recipients WHERE enabled = 1").fetchall()
        ]


def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
