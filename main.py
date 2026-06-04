"""
tg-group-monitor
監聽指定 Telegram 群組，命中關鍵字的訊息即時記錄，並轉發到另一個 TG 對話與 / 或 Webhook。

用個人帳號 (MTProto / Telethon) 登入，適合「你只是群組成員、無法加 bot」的情境。
第一次執行會要求輸入手機驗證碼，成功後產生 .session 檔，之後免重複登入。
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events

import recipients

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


API_ID = _get("API_ID")
API_HASH = _get("API_HASH")
PHONE = _get("PHONE")
SESSION_NAME = _get("SESSION_NAME", "tg_monitor")
SOURCE_CHAT = _get("SOURCE_CHAT")
KEYWORDS = [k.strip().lower() for k in _get("KEYWORDS").split(",") if k.strip()]
BOT_TOKEN = _get("BOT_TOKEN")
BOT_TARGET = _get("BOT_TARGET")
ADMIN_CHAT_ID = _get("ADMIN_CHAT_ID")
WEBHOOK_URL = _get("WEBHOOK_URL")
LOG_TO_FILE = _get("LOG_TO_FILE", "1") == "1"
LOG_FILE = _get("LOG_FILE", "messages.jsonl")
LIST_DIALOGS = _get("LIST_DIALOGS", "0") == "1"
TRADING_ENABLED = _get("TRADING_ENABLED", "0") == "1"


def _parse_chat(value: str):
    """把字串型別的 chat 設定轉成 Telethon 能接受的型別（數字 ID 轉 int，其餘原樣）。"""
    if not value:
        return None
    if value == "me":
        return "me"
    try:
        return int(value)
    except ValueError:
        return value  # @username 或邀請連結


def _matches(text: str) -> bool:
    """沒設關鍵字＝全部命中；有設則只要包含任一關鍵字（不分大小寫）就命中。"""
    if not KEYWORDS:
        return True
    low = text.lower()
    return any(k in low for k in KEYWORDS)


# 報價幣別（交易對結尾），要支援更多就往這裡加
_QUOTE_CURRENCIES = ("USDT", "USDC", "USD", "BUSD", "FDUSD", "TUSD", "DAI")
_QUOTE_ALT = "|".join(_QUOTE_CURRENCIES)
# 交易對符號：基礎幣 + 報價幣（如 VICUSDT、BTCUSDC），前置 # 可有可無
_SYMBOL_RE = re.compile(rf"#?([A-Z0-9]{{2,15}}(?:{_QUOTE_ALT}))\b")
# 在文字中找出「還沒加 #」的交易對（負向回顧避免重複加、避免咬到 URL 內部）
_COIN_RE = re.compile(rf"(?<![#\w])([A-Z0-9]{{2,15}}(?:{_QUOTE_ALT}))\b")

_SIG_VOL = re.compile(r"成交量排名[：:]\s*(\d+)\w*\s*/\s*(\d+)")
_SIG_CAP = re.compile(r"市值[：:]\s*([\d.]+[KMB]?)")
_SIG_RISK = re.compile(r"風險等級[：:]\s*([^\n]+)")
_SIG_ENTRY = re.compile(r"進場價[：:]\s*([\d.]+)")
_SIG_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)")
_SIG_SL = re.compile(r"停損價\s*(\d+)\s*[：:]\s*([\d.]+)")
_SIG_URL = re.compile(r"https?://\S+")

_HIT_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)\s*✅")


def tag_symbols(text: str) -> str:
    """文字中出現 XXXUSDT / XXXUSDC 等交易對、且前面還沒有 # 時自動補上 #；URL 內不更動。"""
    def _sub(seg: str) -> str:
        return _COIN_RE.sub(r"#\1", seg)

    out, last = [], 0
    for m in _SIG_URL.finditer(text):
        out.append(_sub(text[last:m.start()]))
        out.append(m.group(0))  # URL 原樣保留，不在裡面加 #
        last = m.end()
    out.append(_sub(text[last:]))
    return "".join(out)


def parse_signal(text: str) -> dict | None:
    """嘗試把訊號訊息解析成結構化資料。沒命中 symbol+entry 就回 None（代表不是訊號）。"""
    m_symbol = _SYMBOL_RE.search(text)
    m_entry = _SIG_ENTRY.search(text)
    if not (m_symbol and m_entry):
        return None

    entry = float(m_entry.group(1))
    signal: dict = {
        "symbol": m_symbol.group(1),
        "entry": entry,
        "targets": [],
        "stops": [],
    }

    if m := _SIG_VOL.search(text):
        signal["volume_rank"] = f"{m.group(1)}/{m.group(2)}"
    if m := _SIG_CAP.search(text):
        signal["market_cap"] = m.group(1)
    if m := _SIG_RISK.search(text):
        signal["risk"] = m.group(1).strip()
    if m := _SIG_URL.search(text):
        signal["url"] = m.group(0)

    for m in _SIG_TP.finditer(text):
        price = float(m.group(2))
        signal["targets"].append({
            "level": int(m.group(1)),
            "price": price,
            "pct": round((price - entry) / entry * 100, 2),
        })
    for m in _SIG_SL.finditer(text):
        price = float(m.group(2))
        signal["stops"].append({
            "level": int(m.group(1)),
            "price": price,
            "pct": round((price - entry) / entry * 100, 2),
        })
    return signal


def format_signal(signal: dict, when: datetime) -> str:
    """把結構化訊號格式化成自訂版面。"""
    lines = [
        f"⏰ {when.strftime('%Y-%m-%d %H:%M:%S')}",
        f"📊 幣別: {signal['symbol']}",
    ]
    if "risk" in signal:
        lines.append(f"💡 風險: {signal['risk']}")

    meta = []
    if "volume_rank" in signal:
        meta.append(f"24h成交量: {signal['volume_rank']}")
    if "market_cap" in signal:
        meta.append(f"市值: {signal['market_cap']}")
    if meta:
        lines.append("📈 " + " | ".join(meta))

    lines.append("")
    lines.append(f"➡️ 進場: {signal['entry']}")
    for t in signal["targets"]:
        sign = "+" if t["pct"] >= 0 else ""
        lines.append(f"🎯 目標價{t['level']}: {t['price']} ({sign}{t['pct']}%)")

    if signal["stops"]:
        lines.append("")
        for s in signal["stops"]:
            sign = "+" if s["pct"] >= 0 else ""
            lines.append(f"⛔ 停損價{s['level']}: {s['price']} ({sign}{s['pct']}%)")

    if "url" in signal:
        lines.append("")
        lines.append(f"🔗 {signal['url']}")
    return tag_symbols("\n".join(lines))


def parse_target_hit(text: str) -> dict | None:
    """嘗試解析「目標達成通知」（每個目標價後面帶 ✅）。沒有任何命中就回 None。"""
    hits = [
        {"level": int(m.group(1)), "price": float(m.group(2))}
        for m in _HIT_TP.finditer(text)
    ]
    if not hits:
        return None
    m_symbol = _SYMBOL_RE.search(text)
    if not m_symbol:
        return None
    return {"symbol": m_symbol.group(1), "hits": hits}


def format_target_hit(hit: dict, when: datetime) -> str:
    """格式化目標達成通知：每個達成的目標自己一行，✅ 開頭。"""
    lines = [
        f"⏰ {when.strftime('%Y-%m-%d %H:%M:%S')}",
        f"📊 幣別: {hit['symbol']}",
        "",
    ]
    for h in hit["hits"]:
        lines.append(f"✅ 目標價{h['level']}: {h['price']}")
    return tag_symbols("\n".join(lines))


async def list_dialogs(client: TelegramClient) -> None:
    print("=== 你的對話清單（用下面的 id 填入 SOURCE_CHAT）===")
    async for dialog in client.iter_dialogs():
        kind = "群組" if dialog.is_group else ("頻道" if dialog.is_channel else "私訊")
        print(f"[{kind}] {dialog.name!r}  id={dialog.id}")


async def broadcast_via_bot(http: httpx.AsyncClient, text: str) -> None:
    """用 TG Bot 廣播給 recipients 表中所有 enabled 的接收者。"""
    ids = recipients.list_enabled_ids()
    if not ids:
        print("[broadcast] 沒有任何 enabled 接收者，訊息略過")
        return
    for chat_id in ids:
        try:
            resp = await http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"[bot 轉發失敗 {chat_id}] {data.get('description')}")
        except Exception as e:  # 不讓單次失敗中斷監聽
            print(f"[bot 轉發失敗 {chat_id}] {e}")


ADMIN_HELP_TEXT = (
    "管理員指令：\n"
    "/list — 列出所有接收者\n"
    "/add <chat_id> [name] — 新增接收者\n"
    "/remove <chat_id> — 移除接收者\n"
    "/enable <chat_id> — 啟用接收者\n"
    "/disable <chat_id> — 暫停接收者\n"
    "/myid — 顯示自己的 chat_id\n"
    "/help — 顯示此說明"
)
NON_ADMIN_HINT = (
    "你好！\n"
    "你的 chat_id 是 {chat_id}\n"
    "要接收訊號通知，請把這個 chat_id 給管理員開通。"
)


def _format_recipient_line(r: dict) -> str:
    flag = "✅" if r["enabled"] else "⏸️"
    name = f" ({r['name']})" if r.get("name") else ""
    return f"{flag} {r['chat_id']}{name}"


async def _handle_admin_command(event, text: str) -> None:
    parts = text.split(None, 2)  # 切最多 3 段：cmd, arg1, rest
    cmd = parts[0].lower()

    if cmd == "/list":
        items = recipients.list_all()
        if not items:
            await event.reply("(尚無接收者)")
            return
        lines = [f"接收者清單（共 {len(items)} 筆）："]
        lines.extend(_format_recipient_line(r) for r in items)
        await event.reply("\n".join(lines))
        return

    if cmd == "/myid":
        await event.reply(f"你的 chat_id 是 {event.sender_id}")
        return

    if cmd == "/help":
        await event.reply(ADMIN_HELP_TEXT)
        return

    if cmd in ("/add", "/remove", "/enable", "/disable"):
        if len(parts) < 2:
            await event.reply(f"用法：{cmd} <chat_id>" + (" [name]" if cmd == "/add" else ""))
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await event.reply(f"chat_id 必須是數字，收到：{parts[1]!r}")
            return

        if cmd == "/add":
            name = parts[2].strip() if len(parts) >= 3 else None
            if recipients.add(target_id, name):
                tag = f" ({name})" if name else ""
                await event.reply(f"已新增 {target_id}{tag}")
            else:
                await event.reply(f"{target_id} 已存在，不重複新增")
        elif cmd == "/remove":
            if recipients.remove(target_id):
                await event.reply(f"已移除 {target_id}")
            else:
                await event.reply(f"找不到 {target_id}")
        elif cmd == "/enable":
            if recipients.set_enabled(target_id, True):
                await event.reply(f"已啟用 {target_id}")
            else:
                await event.reply(f"找不到 {target_id}")
        elif cmd == "/disable":
            if recipients.set_enabled(target_id, False):
                await event.reply(f"已暫停 {target_id}")
            else:
                await event.reply(f"找不到 {target_id}")
        return

    await event.reply("未知指令，輸入 /help 看可用指令")


async def send_webhook(http: httpx.AsyncClient, payload: dict) -> None:
    try:
        if "discord.com/api/webhooks" in WEBHOOK_URL:
            # Discord 需要 content 欄位：訊號用自訂格式，其他訊息退回原文
            body = payload.get("formatted") or (
                f"**{payload['sender']}** 於 {payload['chat']}\n{payload['text']}"
            )
            await http.post(WEBHOOK_URL, json={"content": body[:1900]})
        else:
            await http.post(WEBHOOK_URL, json=payload)
    except Exception as e:  # 不讓單次失敗中斷監聽
        print(f"[webhook 失敗] {e}")


def write_log(payload: dict) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[寫檔失敗] {e}")


async def main() -> None:
    missing = [n for n in ("API_ID", "API_HASH") if not _get(n)]
    if missing:
        raise SystemExit(f"缺少設定：{', '.join(missing)}，請複製 config.example.env 為 .env 並填寫")

    # 初始化接收者 DB；若 DB 空且設了 BOT_TARGET，自動把 BOT_TARGET 灌進去當第一筆
    recipients.init()
    if recipients.count() == 0 and BOT_TARGET:
        try:
            recipients.add(int(BOT_TARGET), name="bootstrap")
            print(f"[bootstrap] 自動把 BOT_TARGET={BOT_TARGET} 加入接收者清單")
        except ValueError:
            print(f"[bootstrap] BOT_TARGET={BOT_TARGET!r} 不是合法數字，跳過自動加入")

    user_client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    # 有填 PHONE 就用；沒填則讓 Telethon 互動詢問（傳 None 會關掉互動而報錯）
    if PHONE:
        await user_client.start(phone=PHONE)
    else:
        await user_client.start()
    me = await user_client.get_me()
    print(f"已登入：{me.first_name} (@{me.username})")

    if LIST_DIALOGS:
        await list_dialogs(user_client)
        await user_client.disconnect()
        return

    source = _parse_chat(SOURCE_CHAT)
    if source is None:
        raise SystemExit("未設定 SOURCE_CHAT（要監聽的群組）。先把 LIST_DIALOGS=1 跑一次找 id。")

    http = httpx.AsyncClient(timeout=10)

    # 啟動 bot 客戶端（收 admin 指令、回覆非 admin）
    bot_client: TelegramClient | None = None
    admin_id: int | None = None
    if BOT_TOKEN and ADMIN_CHAT_ID:
        try:
            admin_id = int(ADMIN_CHAT_ID)
        except ValueError:
            print(f"[警告] ADMIN_CHAT_ID 不是合法數字：{ADMIN_CHAT_ID!r}，Bot 指令模式停用")
            admin_id = None

    if BOT_TOKEN and admin_id is not None:
        bot_client = TelegramClient("tg_monitor_bot", int(API_ID), API_HASH)
        await bot_client.start(bot_token=BOT_TOKEN)

        @bot_client.on(events.NewMessage(incoming=True))
        async def _bot_router(event):
            if not event.is_private:
                return  # 忽略群組/頻道訊息
            if event.sender_id != admin_id:
                await event.reply(NON_ADMIN_HINT.format(chat_id=event.sender_id))
                return
            text = (event.message.message or "").strip()
            if text:
                await _handle_admin_command(event, text)

    # 啟用幣安現貨自動交易（延遲 import，沒開就不需要裝 python-binance）
    trader = None
    if TRADING_ENABLED:
        import binance_trader as trader
        trader.init()
        await trader.resume()
        trader.start_monitor()

    bot_cmd_status = f"開（admin={admin_id}）" if bot_client else "關"
    print(f"開始監聽：{SOURCE_CHAT}")
    print(f"關鍵字：{KEYWORDS or '（無，全部訊息）'}")
    print(f"Bot 轉發：{'開' if BOT_TOKEN else '關'} | Bot 指令：{bot_cmd_status} | Webhook：{'開' if WEBHOOK_URL else '關'}")
    print(f"接收者：{recipients.count()} 筆 | 自動交易：{'開' if trader else '關'}")

    @user_client.on(events.NewMessage(chats=source))
    async def handler(event: events.NewMessage.Event) -> None:
        text = event.message.message or ""
        if not _matches(text):
            return

        sender = await event.get_sender()
        sender_name = "未知"
        if sender is not None:
            sender_name = (
                getattr(sender, "first_name", None)
                or getattr(sender, "title", None)
                or (f"@{sender.username}" if getattr(sender, "username", None) else str(sender.id))
            )
        chat = await event.get_chat()
        chat_name = getattr(chat, "title", None) or str(event.chat_id)

        now_local = datetime.now(timezone.utc).astimezone()
        signal = parse_signal(text)
        target_hit = None if signal else parse_target_hit(text)
        if signal:
            formatted = format_signal(signal, now_local)
        elif target_hit:
            formatted = format_target_hit(target_hit, now_local)
        else:
            formatted = None

        payload = {
            "time": now_local.isoformat(),
            "chat": chat_name,
            "chat_id": event.chat_id,
            "sender": sender_name,
            "sender_id": getattr(sender, "id", None),
            "message_id": event.message.id,
            "text": text,
            "signal": signal,
            "target_hit": target_hit,
            "formatted": formatted,
        }

        print(f"[命中] {payload['time']} {sender_name}: {text[:80]}")

        if LOG_TO_FILE:
            write_log(payload)

        # 用 Bot 廣播給 recipients 表中所有 enabled 接收者；訊號訊息用自訂格式，其他訊息原樣轉發
        if BOT_TOKEN:
            await broadcast_via_bot(http, formatted or tag_symbols(text))

        if WEBHOOK_URL:
            await send_webhook(http, payload)

        # 只有「解析成功的進場訊號」才自動下單；目標達成通知不下單
        if trader is not None and signal is not None:
            await trader.on_signal(signal)

    try:
        coros = [user_client.run_until_disconnected()]
        if bot_client:
            coros.append(bot_client.run_until_disconnected())
        await asyncio.gather(*coros)
    finally:
        await http.aclose()
        if bot_client:
            await bot_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已停止監聽。")
