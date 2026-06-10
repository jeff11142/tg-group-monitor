"""交易生命週期的 SQLite 持久化。

每筆交易狀態：
  PENDING_BUY — 限價買單已掛、尚未成交
  ACTIVE      — 已成交且 OCO 止盈止損已掛好
  CLOSED      — 已結束（手動或全部成交/止損）
  CANCELED    — 買單未成交被取消

存 signal 原始 dict（JSON）方便成交後重建 OCO。DB 含實際下單資訊，比照 recipients.db 不入版控。
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

DB_FILE = "trades.db"

OPEN_STATUSES = ("PENDING_BUY", "ACTIVE")


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
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT NOT NULL,
                entry        REAL NOT NULL,
                qty          REAL NOT NULL,
                buy_order_id INTEGER,
                status       TEXT NOT NULL,
                signal       TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                oco_orders   TEXT,
                sl_moved     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 舊 DB 遷移：補上後來新增的欄位
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        if "oco_orders" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN oco_orders TEXT")
        if "sl_moved" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN sl_moved INTEGER NOT NULL DEFAULT 0")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["signal"] = json.loads(d["signal"])
    d["oco_orders"] = json.loads(d["oco_orders"]) if d.get("oco_orders") else []
    return d


def add(symbol: str, entry: float, qty: float, buy_order_id: int, signal: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO trades (symbol, entry, qty, buy_order_id, status, signal, created_at) "
            "VALUES (?, ?, ?, ?, 'PENDING_BUY', ?, ?)",
            (symbol, entry, qty, buy_order_id,
             json.dumps(signal, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def get(trade_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        return _row_to_dict(row) if row else None


def set_status(trade_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))


def set_qty(trade_id: int, qty: float) -> None:
    with _conn() as conn:
        conn.execute("UPDATE trades SET qty = ? WHERE id = ?", (qty, trade_id))


def set_oco(trade_id: int, oco_orders: list) -> None:
    with _conn() as conn:
        conn.execute("UPDATE trades SET oco_orders = ? WHERE id = ?",
                     (json.dumps(oco_orders), trade_id))


def update_oco(trade_id: int, oco_orders: list, sl_moved: int) -> None:
    """sl_moved：止損移動的層級（0=未移、1=保本、2=鎖 TP1、3=鎖 TP2…）；布林會轉成 0/1。"""
    with _conn() as conn:
        conn.execute("UPDATE trades SET oco_orders = ?, sl_moved = ? WHERE id = ?",
                     (json.dumps(oco_orders), int(sl_moved), trade_id))


def count_open() -> int:
    with _conn() as conn:
        ph = ",".join("?" * len(OPEN_STATUSES))
        return conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE status IN ({ph})", OPEN_STATUSES
        ).fetchone()[0]


def has_open_symbol(symbol: str) -> bool:
    with _conn() as conn:
        ph = ",".join("?" * len(OPEN_STATUSES))
        return conn.execute(
            f"SELECT 1 FROM trades WHERE symbol = ? AND status IN ({ph}) LIMIT 1",
            (symbol, *OPEN_STATUSES),
        ).fetchone() is not None


def list_status(status: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = ? ORDER BY id", (status,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
