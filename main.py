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
from telethon import Button, TelegramClient, events

import recipients
import signal_store

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


_http: "httpx.AsyncClient | None" = None  # 供 bot 指令處理共用的 HTTP client


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
# 訂閱服務（USDT 付款，管理員手動確認後 /sub 開通）
SUB_PRICE_USDT = _get("SUB_PRICE_USDT", "30")
SUB_DAYS = _get("SUB_DAYS", "30")
SUB_NETWORK = _get("SUB_NETWORK", "TRC20")
SUB_WALLET = _get("SUB_WALLET")
SUB_CHECK_INTERVAL = int(_get("SUB_CHECK_INTERVAL", "300"))


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
# 基礎幣長度下限為 1，支援單字元代號（如 4USDT、XUSDT）
_SYMBOL_RE = re.compile(rf"#?([A-Z0-9]{{1,15}}(?:{_QUOTE_ALT}))\b")
# 在文字中找出「還沒加 #」的交易對（負向回顧避免重複加、避免咬到 URL 內部）
_COIN_RE = re.compile(rf"(?<![#\w])([A-Z0-9]{{1,15}}(?:{_QUOTE_ALT}))\b")

_SIG_VOL = re.compile(r"成交量排名[：:]\s*(\d+)\w*\s*/\s*(\d+)")
_SIG_CAP = re.compile(r"市值[：:]\s*([\d.]+[KMB]?)")
_SIG_RISK = re.compile(r"風險等級[：:]\s*([^\n]+)")
_SIG_ENTRY = re.compile(r"進場價[：:]\s*([\d.]+)")
_SIG_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)")
_SIG_SL = re.compile(r"停損價\s*(\d+)\s*[：:]\s*([\d.]+)")
_SIG_URL = re.compile(r"https?://\S+")

_HIT_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)\s*✅")
_HIT_SL = re.compile(r"停損價\s*(\d+)\s*[：:]\s*([\d.]+)")


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
    m_symbol = _SYMBOL_RE.search(_SIG_URL.sub("", text))
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


def _our_sl1(signal: dict) -> dict | None:
    """算出我們自己的單一 SL1：沿用交易模組「訊號SL1 與 SL1_PCT 上限取小」的計算。
    接收訊號當下呼叫一次、覆蓋 signal['stops']，之後通知/廣播/存DB/下單全程沿用，不再碰訊號源。
    交易模組不可用（純通知部署）時退回訊號原始 SL1。"""
    try:
        import binance_trader as bt
        return bt.effective_sl1(signal)
    except Exception:
        stops = signal.get("stops") or []
        return stops[0] if stops else None


def _raw_signal_mode() -> bool:
    """是否為「原始訊號直通」模式：直接用訊號原始 SL1/SL2＋TP 掛單，不做過濾重算。
    優先讀交易模組的即時旗標（/config 可切換）；純通知部署無交易模組時退回 .env。"""
    try:
        import binance_trader as bt
        return bt.RAW_SIGNAL_MODE
    except Exception:
        return _get("RAW_SIGNAL_MODE", "0") == "1"


def _make_admin_notifier(http: "httpx.AsyncClient", admin_id: int | None):
    """產生交易事件通知函式（注入 binance_trader）：寫入 log 留底 + 私訊管理員。
    僅發給管理員（這是操作者自己帳號的倉位管理事件，不廣播給訂閱者）。"""
    async def _notify(text: str) -> None:
        now = datetime.now(timezone.utc).astimezone()
        if LOG_TO_FILE:
            write_log({"time": now.isoformat(), "type": "trade_event", "text": text})
        if admin_id:
            await send_bot_dm(http, admin_id, text)
        else:
            print("[trade-notify] 未設 ADMIN_CHAT_ID，僅 log 不私訊")
    return _notify


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
    m_symbol = _SYMBOL_RE.search(_SIG_URL.sub("", text))
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


def parse_stop_hit(text: str) -> dict | None:
    """嘗試解析「觸發停損通知」（只有停損價、沒有進場價/目標價）。沒有命中就回 None。"""
    hits = [
        {"level": int(m.group(1)), "price": float(m.group(2))}
        for m in _HIT_SL.finditer(text)
    ]
    if not hits:
        return None
    m_symbol = _SYMBOL_RE.search(_SIG_URL.sub("", text))
    if not m_symbol:
        return None
    return {"symbol": m_symbol.group(1), "hits": hits}


def format_stop_hit(hit: dict, when: datetime) -> str:
    """格式化觸發停損通知：圖示沿用訊號裡的 ⛔，版面與其他通知一致。"""
    lines = [
        f"⏰ {when.strftime('%Y-%m-%d %H:%M:%S')}",
        f"📊 幣別: {hit['symbol']}",
        "",
    ]
    for h in hit["hits"]:
        lines.append(f"⛔ 停損價{h['level']}: {h['price']}")
    return tag_symbols("\n".join(lines))


async def list_dialogs(client: TelegramClient) -> None:
    print("=== 你的對話清單（用下面的 id 填入 SOURCE_CHAT）===")
    async for dialog in client.iter_dialogs():
        kind = "群組" if dialog.is_group else ("頻道" if dialog.is_channel else "私訊")
        print(f"[{kind}] {dialog.name!r}  id={dialog.id}")


async def send_bot_dm(http: httpx.AsyncClient, chat_id: int, text: str,
                      reply_to: int | None = None) -> int | None:
    """用 TG Bot 私訊單一 chat_id。回傳送出訊息的 message_id（失敗回 None）。
    reply_to：要引用回覆的 message_id（原訊息已刪也照常送，不報錯）。"""
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = True
    try:
        resp = await http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[bot 發送失敗 {chat_id}] {data.get('description')}")
            return None
        return data["result"]["message_id"]
    except Exception as e:  # 不讓單次失敗中斷監聽
        print(f"[bot 發送失敗 {chat_id}] {e}")
        return None


PUBLIC_COMMANDS = [
    {"command": "myid", "description": "顯示你的編號"},
    {"command": "help", "description": "顯示使用說明"},
]
ADMIN_COMMANDS = [
    {"command": "sub", "description": "開通或續期訂閱"},
    {"command": "unsub", "description": "停用某人的訂閱"},
    {"command": "subs", "description": "列出所有訂閱狀態"},
    {"command": "add", "description": "新增接收者"},
    {"command": "remove", "description": "移除接收者"},
    {"command": "enable", "description": "啟用接收者"},
    {"command": "disable", "description": "暫停接收者"},
    {"command": "list", "description": "列出所有接收者"},
    {"command": "config", "description": "查看或調整交易參數"},
    {"command": "myid", "description": "顯示你的編號"},
    {"command": "help", "description": "顯示管理說明"},
]
# 管理專用指令（非管理員輸入這些 → 靜默忽略，不回覆）
ADMIN_ONLY_CMDS = ({"/" + c["command"] for c in ADMIN_COMMANDS}
                   - {"/" + c["command"] for c in PUBLIC_COMMANDS})


async def setup_bot_commands(http: httpx.AsyncClient, admin_id: int | None) -> None:
    """設定 bot 的 / 指令選單：一般人只看到 PUBLIC，管理員額外看到 ADMIN 指令。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    try:
        # 預設（所有人）
        await http.post(url, json={"commands": PUBLIC_COMMANDS})
        # 管理員專屬（限定 admin 的私訊）
        if admin_id is not None:
            await http.post(url, json={
                "commands": ADMIN_COMMANDS,
                "scope": {"type": "chat", "chat_id": admin_id},
            })
        print("[bot] 指令選單已設定（一般／管理員分流）")
    except Exception as e:
        print(f"[bot] 設定指令選單失敗：{e}")


async def broadcast_via_bot(http: httpx.AsyncClient, text: str,
                            reply_map: dict[int, int] | None = None) -> dict[int, int]:
    """用 TG Bot 廣播給「已啟用且未到期」的訂閱者。
    reply_map：{chat_id: 要回覆引用的 message_id}，讓 TP/SL 通知掛在該位的進場訊息下。
    回傳 {chat_id: 本次送出的 message_id}，供記錄。"""
    ids = recipients.list_active_ids()
    if not ids:
        print("[broadcast] 沒有任何有效訂閱者，訊息略過")
        return {}
    sent: dict[int, int] = {}
    for chat_id in ids:
        mid = await send_bot_dm(http, chat_id, text, reply_to=(reply_map or {}).get(chat_id))
        if mid:
            sent[chat_id] = mid
    return sent


async def _broadcast_signal(http: httpx.AsyncClient, text: str, signal: dict) -> None:
    """廣播進場訊號，存進 signal_store，並記錄每位收件人的 message_id（供 TP/SL 通知回覆引用）。"""
    meta = {k: signal[k] for k in ("risk", "volume_rank", "market_cap", "url") if k in signal}
    if signal.get("source_stops"):
        meta["source_stops"] = signal["source_stops"]  # 訊號源原始 SL1/SL2（自算 SL1 覆蓋前的備份）
    sig_id = signal_store.add_signal(
        signal["symbol"], signal["entry"], signal["targets"], signal["stops"], meta=meta,
    )
    sent = await broadcast_via_bot(http, text)
    for cid, mid in sent.items():
        signal_store.record_message(sig_id, cid, mid)


async def _notify_hit(http: httpx.AsyncClient, signal_id: int, text: str) -> None:
    """TP/SL 達標通知：廣播給訂閱者，逐位回覆引用其進場訊息（使用者可一鍵跳回）。"""
    await broadcast_via_bot(http, text, reply_map=signal_store.messages_for(signal_id))


async def _check_signal_hits(http: httpx.AsyncClient, prices: dict[str, float]) -> None:
    """拿全市場標記價，比對每筆 open 訊號的 TP/SL（做多：價≥TP 觸發、價≤SL 觸發），
    每個層級只通知一次；觸發停損或全部 TP 達標 → 收尾該訊號。"""
    now = datetime.now(timezone.utc).astimezone()
    for sig in signal_store.open_signals():
        px = prices.get(sig["symbol"])
        if px is None:
            continue
        tp_done = set(sig["notified"].get("tp", []))
        sl_done = set(sig["notified"].get("sl", []))
        tp_hit_now = False
        for t in sig["targets"]:
            if t["level"] not in tp_done and px >= t["price"]:
                hit = {"symbol": sig["symbol"], "hits": [{"level": t["level"], "price": t["price"]}]}
                await _notify_hit(http, sig["id"], format_target_hit(hit, now))
                signal_store.record_hit(sig["id"], "tp", t["level"])
                tp_done.add(t["level"])
                tp_hit_now = True
                print(f"[watch] {sig['symbol']} TP{t['level']} 達標 @ {px}")
        # 訊號已達 TP：若我們掛在低點的限價進場單還沒成交，追高沒意義 → 撤掉未成交進場單、釋放額度
        if tp_hit_now and TRADING_ENABLED:
            try:
                import binance_trader as bt
                await bt.cancel_pending_entry(sig["symbol"], reason="訊號已達 TP")
            except Exception as e:
                print(f"[watch] 撤未成交進場單失敗 {sig['symbol']}：{e!r}")
        sl_triggered = False
        for s in sig["stops"]:
            if s["level"] not in sl_done and px <= s["price"]:
                hit = {"symbol": sig["symbol"], "hits": [{"level": s["level"], "price": s["price"]}]}
                await _notify_hit(http, sig["id"], format_stop_hit(hit, now))
                signal_store.record_hit(sig["id"], "sl", s["level"])
                print(f"[watch] {sig['symbol']} SL{s['level']} 觸發 @ {px}")
                signal_store.close_signal(sig["id"])   # 觸發停損 → 收尾，停止監聽
                sl_triggered = True
                break
        if not sl_triggered and sig["targets"] and all(t["level"] in tp_done for t in sig["targets"]):
            signal_store.close_signal(sig["id"])       # 全部 TP 達標 → 收尾
            print(f"[watch] {sig['symbol']} 全部 TP 達標 → 收尾")


async def _price_watch_loop(http: httpx.AsyncClient) -> None:
    """公開行情 WS（主網全市場標記價，免金鑰）：偵測 open 訊號的 TP/SL 觸發並通知。
    注意：一律用主網真價，因為訊號的 TP/SL 是真實市場價位（與自動交易是否 testnet 無關）。"""
    try:
        from binance import AsyncClient, BinanceSocketManager
    except ImportError:
        print("[watch] 未安裝 python-binance，TP/SL 行情監聽停用")
        return
    backoff = 1
    while True:
        client = None
        try:
            # 不用 AsyncClient.create()：它先建 session 再 ping，ping 失敗時 session 會洩漏；
            # 改為先建構（同步、必定可關閉），ping 失敗也會走 finally 關閉
            client = AsyncClient()   # 主網公開行情，不需金鑰
            await client.ping()
            bsm = BinanceSocketManager(client)
            async with bsm.all_mark_price_socket(fast=True) as stream:
                print("[watch] 行情 WS 已連線，監聽訊號 TP/SL（標記價）")
                backoff = 1
                while True:
                    msg = await stream.recv()
                    arr = msg.get("data") if isinstance(msg, dict) else msg
                    if not isinstance(arr, list):
                        continue
                    prices = {it["s"]: float(it["p"]) for it in arr if it.get("s") and it.get("p")}
                    await _check_signal_hits(http, prices)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[watch] 行情 WS 中斷，{backoff}s 後重連：{e!r}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if client is not None:
                try:
                    await client.close_connection()
                except Exception:
                    pass


async def _subscription_loop() -> None:
    """定期檢查訂閱到期：過期者停用、推播停止，並通知本人。"""
    while True:
        try:
            for r in recipients.due_expired():
                recipients.set_enabled(r["chat_id"], False)
                print(f"[訂閱] {r['chat_id']} 已到期，停止推播")
                if _http is not None:
                    await send_bot_dm(_http, r["chat_id"],
                                      "⌛ 你的訂閱已到期，已停止訊號推播。\n"
                                      "若要續訂，請私訊本 bot 取得付款資訊。")
        except Exception as e:
            print(f"[訂閱] 到期檢查錯誤：{e}")
        await asyncio.sleep(SUB_CHECK_INTERVAL)


ADMIN_HELP_TEXT = (
    "管理指令（點選後依提示輸入即可）：\n"
    "/sub — 開通或續期訂閱\n"
    "/unsub — 停用某人的訂閱\n"
    "/subs — 列出所有訂閱狀態\n"
    "/list — 列出所有接收者\n"
    "/add — 新增接收者\n"
    "/remove — 移除接收者\n"
    "/enable — 啟用接收者\n"
    "/disable — 暫停接收者\n"
    "/config — 查看或調整交易參數\n"
    "/cancel — 取消進行中的操作\n"
    "/myid — 顯示你的編號\n"
    "/help — 顯示此說明"
)

# 一般使用者（非管理員）的 /help 說明
PUBLIC_HELP_TEXT = (
    "👋 這是交易訊號通知 Bot\n"
    "訂閱後即可即時收到篩選過的交易訊號。\n\n"
    "可用指令：\n"
    "/myid — 顯示你的編號（開通訂閱時要提供給管理員）\n"
    "/help — 顯示這份說明\n\n"
    f"📦 訂閱方案：{SUB_PRICE_USDT} USDT / {SUB_DAYS} 天\n"
    "・直接傳任何訊息給我，即可看到你的 chat_id、目前訂閱狀態與付款方式\n"
    "・付款後把你的 chat_id 與交易截圖 / TxID 傳給管理員即可開通"
)


def _remaining_text(expires_at: str | None) -> str:
    """把到期日轉成人看得懂的剩餘天數描述。"""
    if not expires_at:
        return "永久"
    exp = datetime.fromisoformat(expires_at)
    now = datetime.now(timezone.utc)
    if exp <= now:
        return "已到期"
    days = (exp - now).days
    return f"到 {exp.astimezone().strftime('%Y-%m-%d')}（剩 {days} 天）"


def subscription_hint(chat_id: int) -> str:
    """非管理員私訊 bot 時的回覆：chat_id + 訂閱狀態 / 付款指引。"""
    r = recipients.get(chat_id)
    lines = ["你好！", f"你的編號是 {chat_id}", ""]
    if r and r["enabled"] and (r["expires_at"] is None or
                               datetime.fromisoformat(r["expires_at"]) > datetime.now(timezone.utc)):
        lines.append(f"✅ 你的訂閱狀態：{_remaining_text(r['expires_at'])}")
    else:
        lines.append(f"📦 訂閱方案：{SUB_PRICE_USDT} USDT / {SUB_DAYS} 天")
        if SUB_WALLET:
            lines.append(f"💰 轉帳 USDT（{SUB_NETWORK}）到：")
            lines.append(SUB_WALLET)
        lines.append("")
        lines.append(f"付款後請把「你的編號（{chat_id}）」與交易截圖 / TxID 傳給管理員，即可開通。")
    return "\n".join(lines)


def _format_recipient_line(r: dict) -> str:
    flag = "✅" if r["enabled"] else "⏸️"
    name = f" ({r['name']})" if r.get("name") else ""
    return f"{flag} {r['chat_id']}{name} — {_remaining_text(r.get('expires_at'))}"


def _v_leverage(v: float) -> str | None:
    return None if 1 <= v <= 125 else "槓桿需在 1~125 之間"


def _v_positive(v: float) -> str | None:
    return None if v > 0 else "必須大於 0"


def _v_max_open(v: float) -> str | None:
    return None if v >= 1 else "至少要 1"


def _v_nonneg(v: float) -> str | None:
    return None if v >= 0 else "不能小於 0"


# 可由 Bot 即時調整的交易參數：
#   bot_key -> (binance_trader 模組屬性, .env 鍵名, 轉型函式, 範圍檢查)
TUNABLE_PARAMS = {
    "leverage":          ("LEVERAGE",          "LEVERAGE",          int,   _v_leverage),
    "min_amount_mult":   ("MIN_AMOUNT_MULT",   "MIN_AMOUNT_MULT",   float, _v_positive),
    "max_open_trades":   ("MAX_OPEN_TRADES",   "MAX_OPEN_TRADES",   int,   _v_max_open),
    "entry_timeout_min": ("ENTRY_TIMEOUT_MIN", "ENTRY_TIMEOUT_MIN", float, _v_nonneg),
    "max_hold_hours":    ("MAX_HOLD_HOURS",    "MAX_HOLD_HOURS",    float, _v_nonneg),
}

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _fmt_val(v) -> str:
    """整數值的 float 顯示成 10 而非 10.0。"""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _update_env_file(env_key: str, value_str: str) -> None:
    """只改 .env 中 env_key 那一行的值（找不到就附加），保留其餘內容與註解。"""
    new_line = f"{env_key}={value_str}\n"
    try:
        with open(_ENV_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    for i, ln in enumerate(lines):
        s = ln.lstrip()
        if s.startswith(f"{env_key}=") and not s.startswith("#"):
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


# 每個可調參數的說明文字（按下按鈕後回給管理員，提示要輸入什麼）
PARAM_HELP = {
    "leverage": "槓桿倍數，整數 1~125。每筆進場前套用。",
    "min_amount_mult": "最小金額倍數，>0 的數。最小可下量 × 此倍數 = 投入保證金本金。",
    "max_open_trades": "最大同時持倉筆數，整數 ≥1。",
    "entry_timeout_min": "進場掛單幾分鐘未成交就撤單，數字 ≥0；0 = 永不超時、一直等成交。",
}

# admin sender_id -> 進行中的互動：("param", 參數key) 或 ("cmd", 指令名)
_pending: dict[int, tuple[str, str]] = {}

# 需要打字輸入的管理指令（新對象／需要天數）：點一下後 Bot 發提示並監聽你的回覆
INTERACTIVE_PROMPTS = {
    "sub": "請輸入要開通訂閱的對象編號、天數，名稱可省略，用空格分隔。\n例如：123456789 30 小明",
    "add": "請輸入要新增的接收者編號，名稱可省略，用空格分隔。\n例如：123456789 小明",
}

# 對「既有接收者」操作的指令：點一下後列出名單按鈕，點某人即執行
_TARGET_ACTION_LABEL = {
    "remove": "移除", "enable": "啟用", "disable": "暫停", "unsub": "停用訂閱",
}


def _do_target_action(action: str, chat_id: int) -> str:
    """對既有接收者執行單一操作，回傳結果訊息。"""
    if action == "remove":
        return f"已移除 {chat_id}" if recipients.remove(chat_id) else f"找不到 {chat_id}"
    if action == "enable":
        return f"已啟用 {chat_id}" if recipients.set_enabled(chat_id, True) else f"找不到 {chat_id}"
    if action == "disable":
        return f"已暫停 {chat_id}" if recipients.set_enabled(chat_id, False) else f"找不到 {chat_id}"
    if action == "unsub":
        return f"已停用 {chat_id} 的訂閱" if recipients.set_enabled(chat_id, False) else f"找不到 {chat_id}"
    return "未知操作"


async def _send_target_panel(event, action: str) -> None:
    """列出目前接收者，每位一顆按鈕，點下去即對該位執行 action。"""
    items = recipients.list_all()
    if not items:
        await event.reply("目前沒有任何接收者。")
        return
    buttons = []
    for r in items:
        cid = r["chat_id"]
        status = "啟用中" if r["enabled"] else "已暫停"
        name = f"{r['name']}｜" if r["name"] else ""
        buttons.append([Button.inline(f"{name}{cid}（{status}）", f"act:{action}:{cid}".encode())])
    buttons.append(_cancel_row())
    await event.reply(f"請選擇要{_TARGET_ACTION_LABEL[action]}的對象：", buttons=buttons)


async def _send_renew_panel(event) -> None:
    """/sub：列出訂閱中的對象供續訂（點→確認→續訂）；也可直接打字開通新對象。"""
    subs = [r for r in recipients.list_all() if r["expires_at"]]
    if not subs:
        await event.reply(
            "目前沒有訂閱中的對象。\n要開通新訂閱，請直接輸入：對象編號 天數 名稱"
            "（名稱可省略）。\n例如：123456789 30 小明",
            buttons=[_cancel_row()],
        )
        return
    buttons = []
    for r in subs:
        cid = r["chat_id"]
        name = f"{r['name']}｜" if r["name"] else ""
        buttons.append([Button.inline(f"{name}{cid}（{_remaining_text(r['expires_at'])}）",
                                      f"renew:{cid}".encode())])
    buttons.append(_cancel_row())
    await event.reply(
        f"請選擇要續訂的對象（每次續 {SUB_DAYS} 天）；\n"
        "若要開通新對象，直接輸入：對象編號 天數 名稱（名稱可省略）。",
        buttons=buttons,
    )


def _type_label(conv) -> str:
    return "整數" if conv is int else "數字"


def _apply_param(key: str, raw: str) -> tuple[bool, str]:
    """驗證並套用單一參數：同步改 runtime 全域 + 寫回 .env。回傳 (成功, 回覆訊息)。"""
    attr, env_key, conv, check = TUNABLE_PARAMS[key]
    raw = raw.strip()
    try:
        value = conv(raw)
    except ValueError:
        return False, (f"❌ {key} 需要{_type_label(conv)}，收到 {raw!r}，"
                       "請重新輸入（/cancel 取消）")
    err = check(value)
    if err:
        return False, f"❌ {key} {err}（收到 {_fmt_val(value)}），請重新輸入（/cancel 取消）"
    import binance_trader as bt
    old = getattr(bt, attr)
    setattr(bt, attr, value)
    _update_env_file(env_key, _fmt_val(value))
    return True, (f"✅ {key}：{_fmt_val(old)} → {_fmt_val(value)}"
                  "（已寫回 .env，下一筆新進場生效；已開倉位不受影響）")


def _cancel_row() -> list:
    """互動面板／提示最後都附這顆取消按鈕。"""
    return [Button.inline("✖️ 取消", b"cancel")]


async def _send_config_panel(event) -> None:
    """顯示目前交易參數，每個可調參數附一顆 inline 按鈕。"""
    import binance_trader as bt
    lines = ["📊 目前交易參數（點下方按鈕修改）："]
    buttons = []
    for key, (attr, _, _, _) in TUNABLE_PARAMS.items():
        lines.append(f"  {key} = {_fmt_val(getattr(bt, attr))}")
        buttons.append([Button.inline(f"✏️ 改 {key}", f"setparam:{key}".encode())])
    sl2_on = bt.SL2_MULT > 0
    sl2_desc = f"開（SL2 = SL1 × {_fmt_val(bt.SL2_MULT)}）" if sl2_on else "關（單一止損守全倉）"
    lines.append(f"  二段止損(SL2) = {sl2_desc}")
    buttons.append([Button.inline(f"🛑 二段止損：點此{'關閉' if sl2_on else '開啟'}", b"sl2tog")])
    be_on = bt.BREAKEVEN_AFTER_TP1
    be_desc = "開（TP 成交後止損逐段上移鎖利）" if be_on else "關（止損固定在 SL1 不動）"
    lines.append(f"  動態止損 = {be_desc}")
    buttons.append([Button.inline(f"📈 動態止損：點此{'關閉' if be_on else '開啟'}", b"betog")])
    raw_on = bt.RAW_SIGNAL_MODE
    raw_desc = "開（直接用訊號原始 SL1/SL2 掛單）" if raw_on else "關（用過濾重算的 SL 階梯）"
    lines.append(f"  原始訊號做單 = {raw_desc}")
    buttons.append([Button.inline(f"📡 原始訊號做單：點此{'關閉' if raw_on else '開啟'}", b"rawtog")])
    auto = "開" if bt.AUTO_MIN_AMOUNT else "關"
    lines.append(f"（環境：{bt.current_network()}｜自動最小金額：{auto}）")
    target = "正式網（真錢）" if bt.TESTNET else "測試網"
    buttons.append([Button.inline(f"🔀 切換到{target}", b"netsw")])
    buttons.append(_cancel_row())
    await event.respond("\n".join(lines), buttons=buttons)


async def _handle_admin_command(event, text: str) -> None:
    parts = text.split(None, 2)  # 切最多 3 段：cmd, arg1, rest
    cmd = parts[0].lower()

    if cmd == "/config":
        await _send_config_panel(event)
        return

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
        await event.reply(f"你的編號是 {event.sender_id}")
        return

    if cmd == "/help":
        await event.reply(ADMIN_HELP_TEXT)
        return

    if cmd == "/subs":
        items = recipients.list_all()
        if not items:
            await event.reply("(尚無訂閱者)")
            return
        lines = [f"訂閱者（共 {len(items)} 筆）："]
        lines.extend(_format_recipient_line(r) for r in items)
        await event.reply("\n".join(lines))
        return

    if cmd == "/sub":
        if len(parts) < 3:
            await event.reply("請輸入：對象編號 天數 名稱（名稱可省略），用空格分隔。")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await event.reply(f"編號必須是數字，收到：{parts[1]!r}")
            return
        rest = parts[2].split(None, 1)
        try:
            days = int(rest[0])
        except ValueError:
            await event.reply(f"天數必須是數字，收到：{rest[0]!r}")
            return
        name = rest[1].strip() if len(rest) > 1 else None
        new_exp = recipients.subscribe(target_id, days, name)
        await event.reply(f"已開通 {target_id} +{days} 天，{_remaining_text(new_exp)}")
        # 通知訂閱者本人（若曾與 bot 互動過）
        await send_bot_dm(_http, target_id,
                          f"✅ 訂閱已開通／續期！{_remaining_text(new_exp)}\n你將開始收到訊號通知。")
        return

    if cmd == "/unsub":
        if len(parts) < 2:
            await event.reply("請輸入要停用訂閱的對象編號。")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await event.reply(f"編號必須是數字，收到：{parts[1]!r}")
            return
        await event.reply(_do_target_action("unsub", target_id))
        return

    if cmd in ("/add", "/remove", "/enable", "/disable"):
        if len(parts) < 2:
            await event.reply("請輸入接收者編號" + ("，名稱可省略。" if cmd == "/add" else "。"))
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await event.reply(f"編號必須是數字，收到：{parts[1]!r}")
            return

        if cmd == "/add":
            name = parts[2].strip() if len(parts) >= 3 else None
            if recipients.add(target_id, name):
                tag = f" ({name})" if name else ""
                await event.reply(f"已新增 {target_id}{tag}")
            else:
                await event.reply(f"{target_id} 已存在，不重複新增")
        else:
            await event.reply(_do_target_action(cmd[1:], target_id))
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
    signal_store.init()
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

    global _http
    http = _http = httpx.AsyncClient(timeout=10)

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
            text = (event.message.message or "").strip()
            if event.sender_id != admin_id:
                cmd = text.split()[0].lower().split("@")[0] if text else ""
                if cmd in ADMIN_ONLY_CMDS:
                    return  # 非管理員輸入管理指令 → 無權限，靜默忽略不回覆
                if cmd == "/help":
                    await event.reply(PUBLIC_HELP_TEXT)
                    return
                await event.reply(subscription_hint(event.sender_id))
                return
            if not text:
                return
            sender = event.sender_id
            if text.lower() == "/cancel":
                if _pending.pop(sender, None):
                    await event.reply("已取消。")
                else:
                    await event.reply("目前沒有進行中的操作。")
                return
            # 正在等待輸入（參數值或指令參數），且這次不是新指令 → 當成輸入處理
            pending = _pending.get(sender)
            if pending and not text.startswith("/"):
                kind, key = pending
                if kind == "param":
                    ok, msg = _apply_param(key, text)
                    if ok:
                        _pending.pop(sender, None)
                    await event.reply(msg)
                else:  # "cmd"：把輸入接到指令後面，交給原指令邏輯
                    _pending.pop(sender, None)
                    await _handle_admin_command(event, f"/{key} {text.strip()}")
                return
            # 改打新指令 → 放棄先前等待狀態
            _pending.pop(sender, None)
            name = text.split()[0].lower().split("@")[0][1:]
            # 純點擊（無附帶參數）的指令 → 互動化
            if len(text.split()) == 1:
                if name == "sub":  # 續訂選按鈕，或打字開通新對象
                    _pending[sender] = ("cmd", "sub")
                    await _send_renew_panel(event)
                    return
                if name in INTERACTIVE_PROMPTS:  # 新對象／需天數 → 打字
                    _pending[sender] = ("cmd", name)
                    await event.reply(INTERACTIVE_PROMPTS[name], buttons=[_cancel_row()])
                    return
                if name in _TARGET_ACTION_LABEL:  # 既有對象 → 名單按鈕選人
                    await _send_target_panel(event, name)
                    return
            await _handle_admin_command(event, text)

        @bot_client.on(events.CallbackQuery(pattern=b"setparam:"))
        async def _on_param_button(event):
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            key = event.data.decode().split(":", 1)[1]
            if key not in TUNABLE_PARAMS:
                await event.answer("未知參數")
                return
            import binance_trader as bt
            attr, _, conv, _ = TUNABLE_PARAMS[key]
            _pending[event.sender_id] = ("param", key)
            await event.answer()  # 關掉按鈕的 loading 動畫
            await event.respond(
                f"✏️ 修改 {key}\n"
                f"目前值：{_fmt_val(getattr(bt, attr))}\n"
                f"說明：{PARAM_HELP.get(key, '')}\n\n"
                f"請直接輸入新值（{_type_label(conv)}）。",
                buttons=[_cancel_row()],
            )

        @bot_client.on(events.CallbackQuery(pattern=b"act:"))
        async def _on_action_button(event):
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            try:
                _, action, cid = event.data.decode().split(":", 2)
                chat_id = int(cid)
            except (ValueError, UnicodeDecodeError):
                await event.answer("資料格式錯誤")
                return
            msg = _do_target_action(action, chat_id)
            await event.answer()
            await event.edit(msg)  # 把名單按鈕換成執行結果

        @bot_client.on(events.CallbackQuery(pattern=b"renew:"))
        async def _on_renew_select(event):
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            cid = event.data.decode().split(":", 1)[1]
            r = recipients.get(int(cid))
            await event.answer()
            if not r:
                await event.edit("找不到該對象。")
                return
            who = f"{r['name']}（{cid}）" if r["name"] else cid
            await event.edit(
                f"確認為 {who} 續訂 {SUB_DAYS} 天？\n目前：{_remaining_text(r['expires_at'])}",
                buttons=[[Button.inline("✅ 確認續訂", f"renewok:{cid}".encode())], _cancel_row()],
            )

        @bot_client.on(events.CallbackQuery(pattern=b"renewok:"))
        async def _on_renew_confirm(event):
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            cid = int(event.data.decode().split(":", 1)[1])
            new_exp = recipients.subscribe(cid, int(SUB_DAYS))
            _pending.pop(event.sender_id, None)
            await event.answer("已續訂")
            await event.edit(f"已為 {cid} 續訂 {SUB_DAYS} 天，{_remaining_text(new_exp)}")
            await send_bot_dm(_http, cid,
                              f"✅ 訂閱已續期！{_remaining_text(new_exp)}\n你將持續收到訊號通知。")

        @bot_client.on(events.CallbackQuery(pattern=b"cancel"))
        async def _on_cancel_button(event):
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            _pending.pop(event.sender_id, None)
            await event.answer()
            await event.edit("已取消。")

        @bot_client.on(events.CallbackQuery(pattern=b"netsw"))
        async def _on_net_switch(event):
            """/config 的「切換網路」按鈕：先做安全檢查，再要求二次確認（正式網加重警告）。"""
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            await event.answer()
            if trader is None:
                await event.respond("自動交易未啟用，無法切換網路。")
                return
            import binance_trader as bt
            import trades
            target_testnet = not bt.TESTNET
            target_name = "測試網" if target_testnet else "正式網（真錢）"
            n = trades.count_open()
            if n > 0:
                await event.respond(f"⚠️ 還有 {n} 筆未結倉，請先全部平倉再切換網路"
                                    "（避免舊網路倉位失去自動管理）。")
                return
            if not target_testnet and not bt.mainnet_keys_ready():
                await event.respond("❌ 尚未在 .env 設定正式網 API 金鑰"
                                    "（BINANCE_MAINNET_API_KEY / SECRET），無法切到正式網。")
                return
            warn = ("\n\n⚠️⚠️ 這是【正式網・真錢】，切換後新訊號會用真實資金下單！"
                    if not target_testnet else "")
            flag = "1" if target_testnet else "0"
            await event.respond(
                f"確認從 {bt.current_network()} 切換到「{target_name}」？{warn}",
                buttons=[[Button.inline(f"✅ 確認切到{target_name}", f"netok:{flag}".encode())],
                         _cancel_row()],
            )

        @bot_client.on(events.CallbackQuery(pattern=b"netok:"))
        async def _on_net_confirm(event):
            """確認切換：重建 client/WS 並寫回 .env 持久化。"""
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            if trader is None:
                await event.answer("自動交易未啟用")
                return
            import binance_trader as bt
            target_testnet = event.data.decode().split(":", 1)[1] == "1"
            ok, msg = await bt.switch_network(target_testnet)
            if ok:
                _update_env_file("BINANCE_TESTNET", "1" if target_testnet else "0")
                msg += "（已寫回 .env，重啟後維持此網路）"
            await event.answer()
            await event.edit(msg)

        @bot_client.on(events.CallbackQuery(pattern=b"sl2tog"))
        async def _on_sl2_toggle(event):
            """/config 的「二段止損」開關：SL2_MULT 在 0（單一止損）與 2（雙軌）間切換。"""
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            if trader is None:
                await event.answer("自動交易未啟用")
                return
            import binance_trader as bt
            new_val = 0.0 if bt.SL2_MULT > 0 else 2.0
            bt.SL2_MULT = new_val
            _update_env_file("SL2_MULT", _fmt_val(new_val))
            state = (f"開啟（SL2 = SL1 × {_fmt_val(new_val)}，雙軌半倉）" if new_val > 0
                     else "關閉（單一止損守全倉）")
            await event.answer()
            await event.edit(f"✅ 二段止損已{state}\n"
                             "（已寫回 .env；新進場立即生效，已開倉位在下次止損上移時才轉換）")

        @bot_client.on(events.CallbackQuery(pattern=b"betog"))
        async def _on_be_toggle(event):
            """/config 的「動態止損」開關：BREAKEVEN_AFTER_TP1 在 開/關 間切換。
            開＝TP 成交後止損逐段上移鎖利；關＝止損固定在 SL1 不動。"""
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            if trader is None:
                await event.answer("自動交易未啟用")
                return
            import binance_trader as bt
            new_val = not bt.BREAKEVEN_AFTER_TP1
            bt.BREAKEVEN_AFTER_TP1 = new_val
            _update_env_file("BREAKEVEN_AFTER_TP1", "1" if new_val else "0")
            state = ("開啟（TP 成交後止損逐段上移鎖利）" if new_val
                     else "關閉（止損固定在 SL1 不動）")
            await event.answer()
            await event.edit(f"✅ 動態止損已{state}\n"
                             "（已寫回 .env；新進場立即生效，已開倉位於下次 TP 成交時依新設定處理）")

        @bot_client.on(events.CallbackQuery(pattern=b"rawtog"))
        async def _on_raw_toggle(event):
            """/config 的「原始訊號做單」開關：RAW_SIGNAL_MODE 在 開/關 間切換。
            開＝直接用訊號原始 SL1/SL2＋TP 掛單；關＝用過濾重算的 SL 階梯（SL1_PCT/SL2_MULT）。"""
            if event.sender_id != admin_id:
                await event.answer("無權限", alert=True)
                return
            if trader is None:
                await event.answer("自動交易未啟用")
                return
            import binance_trader as bt
            new_val = not bt.RAW_SIGNAL_MODE
            bt.RAW_SIGNAL_MODE = new_val
            _update_env_file("RAW_SIGNAL_MODE", "1" if new_val else "0")
            state = ("開啟（直接用訊號原始 SL1/SL2＋TP 掛單）" if new_val
                     else "關閉（用過濾重算的 SL 階梯）")
            await event.answer()
            await event.edit(f"✅ 原始訊號做單已{state}\n"
                             "（已寫回 .env；下一筆新訊號立即生效，已開倉位不受影響）")

    # 設定 bot 指令選單（一般／管理員分流）＋ 訂閱到期背景檢查
    if BOT_TOKEN:
        await setup_bot_commands(http, admin_id)
        asyncio.create_task(_subscription_loop())
        asyncio.create_task(_price_watch_loop(http))  # 行情 WS 偵測訊號 TP/SL 並通知

    # 啟用幣安現貨自動交易（延遲 import，沒開就不需要裝 python-binance）
    trader = None
    if TRADING_ENABLED:
        import binance_trader as trader
        trader.init()
        trader.set_notifier(_make_admin_notifier(http, admin_id))  # 交易事件 → 私訊管理員
        await trader.resume()
        trader.start_monitor()
        await trader.start_user_stream()

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
        if signal:
            # 覆蓋前先備份訊號源原始 SL1/SL2，存進 DB 的 meta、並供原始訊號模式下單沿用
            signal["source_stops"] = list(signal.get("stops") or [])
            if not _raw_signal_mode():
                # 過濾重算模式：把停損換成我們算出的單一 SL1（≤SL1_PCT 上限）；通知/廣播/下單全程沿用
                our_sl1 = _our_sl1(signal)
                signal["stops"] = [our_sl1] if our_sl1 else []
            # 原始訊號模式：保留訊號原始 SL1/SL2（通知顯示兩道、下單也照訊號掛）
        target_hit = None if signal else parse_target_hit(text)
        stop_hit = None if (signal or target_hit) else parse_stop_hit(text)
        if signal:
            formatted = format_signal(signal, now_local)
        elif target_hit:
            formatted = format_target_hit(target_hit, now_local)
        elif stop_hit:
            formatted = format_stop_hit(stop_hit, now_local)
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
            "stop_hit": stop_hit,
            "formatted": formatted,
        }

        # 無條件先存原文：不論解析成功與否都落地，確保收到的每則來源訊息都留底（可事後補規則/稽核）
        if LOG_TO_FILE:
            write_log(payload)

        if formatted is None:
            print(f"[略過] {payload['time']} {sender_name}: 解析失敗，已存原文不推送")
            return

        print(f"[命中] {payload['time']} {sender_name}: {text[:80]}")

        # Bot 廣播、Webhook、自動下單並行觸發，互不等待。
        # 只有「進場訊號」用 Bot 廣播給訂閱者；來源的達標/停損訊息不再轉發 —— 改由行情 WS 自行偵測通知。
        jobs = []
        if BOT_TOKEN and signal is not None:
            jobs.append(_broadcast_signal(http, formatted, signal))
        if WEBHOOK_URL:
            jobs.append(send_webhook(http, payload))
        # 只有「解析成功的進場訊號」才自動下單；目標達成通知不下單
        if trader is not None and signal is not None:
            jobs.append(trader.on_signal(signal))
        if jobs:
            await asyncio.gather(*jobs)

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
