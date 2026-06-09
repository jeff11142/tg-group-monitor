"""訊號與「Bot 發送訊息 ID」的 SQLite 持久化。

用途：
1. 儲存每則進場訊號與它的所有止盈/止損點位（含原始 meta）。
2. 記錄「每位收件人收到的進場通知 message_id」，之後 TP/SL 通知可回覆引用該則訊息，
   讓使用者一鍵跳回原始進場訊號。

含他人 chat_id，比照 recipients.db / trades.db 不入版控（.gitignore 已涵蓋 *.db）。
"""

import json
import sqlite3
from datetime import datetime, timezone

DB_FILE = "signals.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol     TEXT NOT NULL,
                entry      REAL NOT NULL,
                targets    TEXT NOT NULL,
                stops      TEXT NOT NULL,
                meta       TEXT,
                status     TEXT NOT NULL DEFAULT 'open',
                notified   TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_messages (
                signal_id  INTEGER NOT NULL,
                chat_id    INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (signal_id, chat_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, id)")
        # 舊 DB 遷移：補上 notified 欄位（記錄已通知過的 TP/SL 層級，避免每秒重複通知）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
        if "notified" not in cols:
            conn.execute("ALTER TABLE signals ADD COLUMN notified TEXT NOT NULL DEFAULT '{}'")


def add_signal(symbol: str, entry: float, targets: list, stops: list,
               meta: dict | None = None) -> int:
    """新增一筆進場訊號，回傳 signal_id。
    同幣舊的 open 會被標記為 'superseded'（保留歷史、只改狀態），
    確保每個幣同時最多一筆 open（引用一律取最新那筆）。"""
    with _conn() as conn:
        conn.execute(
            "UPDATE signals SET status = 'superseded' WHERE symbol = ? AND status = 'open'",
            (symbol,),
        )
        cur = conn.execute(
            "INSERT INTO signals (symbol, entry, targets, stops, meta, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, entry,
             json.dumps(targets, ensure_ascii=False),
             json.dumps(stops, ensure_ascii=False),
             json.dumps(meta or {}, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def record_message(signal_id: int, chat_id: int, message_id: int) -> None:
    """記錄某位收件人收到此訊號進場通知的 message_id（重送則覆蓋）。"""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO signal_messages (signal_id, chat_id, message_id) "
            "VALUES (?, ?, ?)",
            (signal_id, chat_id, message_id),
        )


def latest_signal_id(symbol: str) -> int | None:
    """該交易對最近一筆訊號：優先 status='open'，沒有就退回最新的任一筆。"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM signals WHERE symbol = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1", (symbol,)
        ).fetchone()
        if row:
            return row["id"]
        row = conn.execute(
            "SELECT id FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT 1", (symbol,)
        ).fetchone()
        return row["id"] if row else None


def messages_for(signal_id: int) -> dict[int, int]:
    """回傳 {chat_id: message_id}，供 TP/SL 通知逐位回覆引用。"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT chat_id, message_id FROM signal_messages WHERE signal_id = ?", (signal_id,)
        ).fetchall()
        return {r["chat_id"]: r["message_id"] for r in rows}


def close_signal(signal_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE signals SET status = 'closed' WHERE id = ?", (signal_id,))


def open_signals() -> list[dict]:
    """所有 status='open' 的訊號（含 targets/stops/notified 解析），供行情監聽逐筆比對。"""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM signals WHERE status = 'open' ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["targets"] = json.loads(d["targets"])
        d["stops"] = json.loads(d["stops"])
        d["notified"] = json.loads(d["notified"]) if d.get("notified") else {}
        out.append(d)
    return out


def record_hit(signal_id: int, kind: str, level: int) -> None:
    """記下「某訊號的 TP/SL 某層級已通知過」。kind: 'tp' 或 'sl'。"""
    with _conn() as conn:
        row = conn.execute("SELECT notified FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not row:
            return
        notified = json.loads(row["notified"]) if row["notified"] else {}
        levels = notified.setdefault(kind, [])
        if level not in levels:
            levels.append(level)
        conn.execute("UPDATE signals SET notified = ? WHERE id = ?",
                     (json.dumps(notified), signal_id))


def get_signal(signal_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["targets"] = json.loads(d["targets"])
        d["stops"] = json.loads(d["stops"])
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        d["notified"] = json.loads(d["notified"]) if d.get("notified") else {}
        return d
