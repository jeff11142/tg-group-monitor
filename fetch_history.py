"""抓訊號源頻道的歷史訊息，解析成結構化訊號存成 JSONL（給回測用）。

用獨立的 backtest.session（第一次跑會要手機驗證碼登入），
不碰 tg_monitor.session，不會跟 VPS 上的監聽程式打架。

用法：
    .venv/bin/python fetch_history.py             # 預設抓最近 90 天
    .venv/bin/python fetch_history.py --days 180  # 抓最近 180 天
    .venv/bin/python fetch_history.py --limit 500 # 只抓最近 500 則訊息

輸出：signals_history.jsonl，一行一筆進場訊號：
    {"msg_id": ..., "date": "...", "symbol": ..., "entry": ...,
     "targets": [...], "stops": [...], "raw": "原始訊息全文"}
"""
import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

from main import parse_signal, _parse_chat  # 重用既有解析邏輯，確保跟線上行為一致

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SOURCE_CHAT = _parse_chat(os.getenv("SOURCE_CHAT", ""))

OUT_FILE = "signals_history.jsonl"


async def fetch(days: int, limit: int | None) -> None:
    since = None if limit else datetime.now(timezone.utc) - timedelta(days=days)
    client = TelegramClient("backtest", int(API_ID), API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"已登入：{me.first_name}（backtest.session）")

    total = parsed = 0
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        async for msg in client.iter_messages(SOURCE_CHAT, limit=limit):
            if since and msg.date < since:
                break
            total += 1
            text = msg.text or ""
            sig = parse_signal(text)
            if not sig:
                continue  # 達標通知、停損通知、聊天訊息等都跳過，只留進場訊號
            parsed += 1
            f.write(json.dumps({
                "msg_id": msg.id,
                "date": msg.date.isoformat(),
                "symbol": sig["symbol"],
                "entry": sig["entry"],
                "targets": sig["targets"],
                "stops": sig["stops"],
                "risk": sig.get("risk"),
                "raw": text,
            }, ensure_ascii=False) + "\n")
            if parsed % 50 == 0:
                print(f"  已解析 {parsed} 筆（掃過 {total} 則訊息，最舊 {msg.date:%Y-%m-%d}）")

    await client.disconnect()
    print(f"完成：掃過 {total} 則訊息，解析出 {parsed} 筆進場訊號 → {OUT_FILE}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="抓最近幾天（預設 90）")
    ap.add_argument("--limit", type=int, default=None, help="只抓最近 N 則訊息（設了就忽略 --days）")
    args = ap.parse_args()
    asyncio.run(fetch(args.days, args.limit))
