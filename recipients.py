"""接收者清單的 SQLite 持久化。

DB 檔預設在專案目錄底下的 recipients.db，
透過 Bot 指令動態增減，主程式啟動時若 DB 空且設了 BOT_TARGET 會自動 bootstrap 第一筆。
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_FILE = "recipients.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    # sqlite3 的 `with conn:` 只管交易，不會關閉連線；這裡確保用完一定 close，避免 fd 洩漏
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recipients (
                chat_id    INTEGER PRIMARY KEY,
                name       TEXT,
                enabled    INTEGER NOT NULL DEFAULT 1,
                added_at   TEXT NOT NULL,
                expires_at TEXT
            )
            """
        )
        # 舊 DB 遷移：補上訂閱到期欄位（NULL = 永久，不到期）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(recipients)")}
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE recipients ADD COLUMN expires_at TEXT")


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


def get(chat_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT chat_id, name, enabled, added_at, expires_at FROM recipients WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def list_all() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, name, enabled, added_at, expires_at FROM recipients ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def list_active_ids() -> list[int]:
    """可收訊號的人：已啟用且（永久 或 尚未到期）。"""
    now = _now_iso()
    with _conn() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT chat_id FROM recipients "
                "WHERE enabled = 1 AND (expires_at IS NULL OR expires_at > ?)",
                (now,),
            ).fetchall()
        ]


def subscribe(chat_id: int, days: int, name: str | None = None) -> str:
    """開通或續期：已存在且未到期則從原到期日往後加，否則從現在起算。回傳新的到期日 ISO。"""
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        row = conn.execute(
            "SELECT expires_at FROM recipients WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            base = now
            if row["expires_at"]:
                cur = datetime.fromisoformat(row["expires_at"])
                if cur > now:  # 尚未到期 → 從現有到期日續加
                    base = cur
            new_exp = (base + timedelta(days=days)).isoformat()
            params = [new_exp]
            sql = "UPDATE recipients SET expires_at = ?, enabled = 1"
            if name:
                sql += ", name = ?"
                params.append(name)
            sql += " WHERE chat_id = ?"
            params.append(chat_id)
            conn.execute(sql, params)
        else:
            new_exp = (now + timedelta(days=days)).isoformat()
            conn.execute(
                "INSERT INTO recipients (chat_id, name, enabled, added_at, expires_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (chat_id, name, now.isoformat(), new_exp),
            )
        return new_exp


def due_expired() -> list[dict]:
    """已啟用但已過到期日的訂閱者（供通知並停用）。"""
    now = _now_iso()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, name, expires_at FROM recipients "
            "WHERE enabled = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def count() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
