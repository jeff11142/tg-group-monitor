"""幣安現貨自動化交易：依訊號限價進場，成交後掛 4 張 OCO（多止盈 + SL1 全清）。

策略（靜態）：進場時就把止盈/止損一次掛好，之後交易所自己執行、程式不必盯盤。
- 只做多（現貨無法放空）。
- 把倉位依 TP_RATIOS 拆成數份，每份一張 OCO：止盈腿在各自的目標價、止損腿都在 SL1。
- 價格碰 SL1 時，所有未完成 OCO 的止損腿同時觸發 = 全部清倉。

⚠️ 預設連幣安 testnet（測試網假錢）。確認無誤再把 BINANCE_TESTNET 設 0 上正式網。
"""

import asyncio
import os
from decimal import ROUND_DOWN, Decimal

from binance.client import Client
from binance.exceptions import BinanceAPIException

import trades


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


TESTNET = _get("BINANCE_TESTNET", "1") == "1"
API_KEY = _get("BINANCE_API_KEY")
API_SECRET = _get("BINANCE_API_SECRET")
TRADE_USDT = float(_get("TRADE_USDT", "50"))
MAX_OPEN_TRADES = int(_get("MAX_OPEN_TRADES", "5"))
TP_RATIOS = [float(x) for x in _get("TP_RATIOS", "30,30,20,20").split(",") if x.strip()]
SL_LIMIT_BUFFER_PCT = float(_get("SL_LIMIT_BUFFER_PCT", "0.3"))
POLL_INTERVAL = float(_get("ENTRY_POLL_INTERVAL", "5"))
# 賣單預留的手續費緩衝（%）：用買進數量拆單時扣掉，避免「賣超」實得數量
FEE_BUFFER_PCT = float(_get("SELL_FEE_BUFFER_PCT", "0.1"))

_client: Client | None = None
_filters_cache: dict = {}


def init() -> None:
    global _client
    if not (API_KEY and API_SECRET):
        raise SystemExit("交易已啟用但缺少 BINANCE_API_KEY / BINANCE_API_SECRET")
    _client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    trades.init()
    mode = "TESTNET 測試網" if TESTNET else "⚠️ 正式網（真錢）"
    print(f"[trader] 幣安現貨自動交易啟用：{mode} | 每筆 {TRADE_USDT} USDT | "
          f"最多同時 {MAX_OPEN_TRADES} 筆 | 分批 {TP_RATIOS}")


async def _api(func, **kwargs):
    """python-binance 是同步的，丟到執行緒避免卡住 asyncio 事件迴圈。"""
    return await asyncio.to_thread(func, **kwargs)


def _quantize(value: float, step: str) -> Decimal:
    """把數值往下取整到 step 的倍數（符合幣安 LOT_SIZE / PRICE_FILTER）。"""
    q = Decimal(str(step))
    return ((Decimal(str(value)) // q) * q).quantize(q, rounding=ROUND_DOWN)


def _qstr(value: float, step: str) -> str:
    return format(_quantize(value, step), "f")


async def _get_filters(symbol: str) -> dict | None:
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    info = await _api(_client.get_symbol_info, symbol=symbol)
    if not info:
        return None
    f = {x["filterType"]: x for x in info["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    parsed = {
        "step": f["LOT_SIZE"]["stepSize"],
        "tick": f["PRICE_FILTER"]["tickSize"],
        "min_qty": Decimal(f["LOT_SIZE"]["minQty"]),
        "min_notional": Decimal(notional.get("minNotional", "0")),
        "base": info["baseAsset"],
        "quote": info["quoteAsset"],
    }
    _filters_cache[symbol] = parsed
    return parsed


async def on_signal(signal: dict) -> None:
    """訊號入口；包一層攔截例外，避免交易錯誤中斷監聽主流程。"""
    try:
        await _on_signal(signal)
    except BinanceAPIException as e:
        print(f"[trader] 幣安 API 錯誤：{e.status_code} {e.message}")
    except Exception as e:
        print(f"[trader] on_signal 失敗：{e}")


async def _on_signal(signal: dict) -> None:
    symbol = signal.get("symbol")
    entry = signal.get("entry")
    targets = signal.get("targets") or []
    stops = signal.get("stops") or []
    if not (symbol and entry and targets and stops):
        print(f"[trader] 訊號缺必要欄位（symbol/entry/targets/stops），略過：{symbol}")
        return

    if trades.count_open() >= MAX_OPEN_TRADES:
        print(f"[trader] 已達最大同時持倉 {MAX_OPEN_TRADES}，略過 {symbol}")
        return
    if trades.has_open_symbol(symbol):
        print(f"[trader] {symbol} 已有未結倉，略過重複進場")
        return

    filt = await _get_filters(symbol)
    if not filt:
        print(f"[trader] 找不到交易對 {symbol}（testnet 可能不支援），略過")
        return

    price = _quantize(entry, filt["tick"])
    qty = _quantize(TRADE_USDT / float(price), filt["step"])
    if qty < filt["min_qty"] or qty * price < filt["min_notional"]:
        print(f"[trader] {symbol} 下單量太小（qty={qty}），請調高 TRADE_USDT，略過")
        return

    # 預檢：分批後最小一份是否仍滿足最小金額（否則 OCO 會被幣安拒絕）
    smallest = Decimal(str(min(TP_RATIOS))) / Decimal(str(sum(TP_RATIOS)))
    if qty * price * smallest < filt["min_notional"]:
        print(f"[trader] {symbol} 分批後最小一份 < 最小金額 {filt['min_notional']}，"
              f"請調高 TRADE_USDT，略過")
        return

    order = await _api(_client.order_limit_buy, symbol=symbol,
                       quantity=_qstr(float(qty), filt["step"]),
                       price=_qstr(float(price), filt["tick"]))
    buy_id = order["orderId"]
    tid = trades.add(symbol=symbol, entry=float(price), qty=float(qty),
                     buy_order_id=buy_id, signal=signal)
    print(f"[trader] {symbol} 限價買單已掛 @ {price}（qty={qty}，trade#{tid}），等待成交…")
    asyncio.create_task(_watch_and_protect(tid, order.get("status", "")))


async def _watch_and_protect(tid: int, initial_status: str = "") -> None:
    """等買單成交（已成交就跳過輪詢），成交後掛 OCO 止盈止損格。"""
    try:
        trade = trades.get(tid)
        symbol = trade["symbol"]
        order_id = trade["buy_order_id"]

        if initial_status != "FILLED":
            # 剛下單時 testnet 可能因複寫延遲回 -2013（其實單子在），容忍重試
            not_found = 0
            await asyncio.sleep(1.0)
            while True:
                try:
                    order = await _api(_client.get_order, symbol=symbol, orderId=order_id)
                except BinanceAPIException as e:
                    if e.code == -2013 and not_found < 10:
                        not_found += 1
                        await asyncio.sleep(POLL_INTERVAL)
                        continue
                    raise
                status = order["status"]
                if status == "FILLED":
                    break
                if status in ("CANCELED", "REJECTED", "EXPIRED"):
                    trades.set_status(tid, "CANCELED")
                    print(f"[trader] {symbol} 買單未成交（{status}），trade#{tid} 取消")
                    return
                await asyncio.sleep(POLL_INTERVAL)

        await _place_oco_grid(trade)
        trades.set_status(tid, "ACTIVE")
        print(f"[trader] {symbol} 已成交，OCO 止盈止損掛好，trade#{tid} → ACTIVE")
    except BinanceAPIException as e:
        print(f"[trader] trade#{tid} 掛 OCO 失敗：{e.status_code} {e.message}")
    except Exception as e:
        print(f"[trader] trade#{tid} 監控失敗：{e}")


async def _place_oco_grid(trade: dict) -> None:
    symbol = trade["symbol"]
    signal = trade["signal"]
    filt = await _get_filters(symbol)
    targets = signal["targets"]
    sl1 = signal["stops"][0]["price"]
    sl_trigger = _quantize(sl1, filt["tick"])
    # 止損腿是 stop-limit，限價設得比觸發價低一點，確保急殺時掛得掉
    sl_limit = _quantize(sl1 * (1 - SL_LIMIT_BUFFER_PCT / 100), filt["tick"])

    # 只用「這筆買單成交的數量」拆分（不是帳戶總餘額，避免賣到既有持倉），
    # 並扣掉手續費緩衝避免賣超實得數量
    bought = Decimal(str(trade["qty"])) * Decimal(str(1 - FEE_BUFFER_PCT / 100))
    sellable = _quantize(float(bought), filt["step"])

    n = min(len(TP_RATIOS), len(targets))
    ratios = TP_RATIOS[:n]
    total = Decimal(str(sum(ratios)))
    step = Decimal(str(filt["step"]))

    portions: list[Decimal] = []
    allocated = Decimal("0")
    for i in range(n):
        if i < n - 1:
            qty_i = _quantize(float(sellable * Decimal(str(ratios[i])) / total), filt["step"])
        else:
            qty_i = ((sellable - allocated) // step) * step  # 最後一份吃剩餘，避免 dust
        allocated += qty_i
        portions.append(qty_i)

    for i in range(n):
        qty_i = portions[i]
        if qty_i <= 0:
            continue
        tp_price = _quantize(targets[i]["price"], filt["tick"])
        # 新版 OCO endpoint（orderList/oco）：SELL 時 above=止盈(LIMIT_MAKER)、below=止損(STOP_LOSS_LIMIT)
        await _api(
            _client.create_oco_order,
            symbol=symbol,
            side="SELL",
            quantity=format(qty_i, "f"),
            aboveType="LIMIT_MAKER",
            abovePrice=format(tp_price, "f"),
            belowType="STOP_LOSS_LIMIT",
            belowStopPrice=format(sl_trigger, "f"),
            belowPrice=format(sl_limit, "f"),
            belowTimeInForce="GTC",
        )
        print(f"[trader]   OCO{i + 1}: 賣 {qty_i} @ TP {tp_price} / SL {sl_trigger}")


async def resume() -> None:
    """程式重啟時，把仍在等成交的買單重新接上監控。"""
    for t in trades.list_status("PENDING_BUY"):
        print(f"[trader] 回復未完成交易 trade#{t['id']} {t['symbol']}，繼續等待成交…")
        asyncio.create_task(_watch_and_protect(t["id"]))
