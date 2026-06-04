"""幣安自動化交易：依訊號限價進場，成交後掛多段止盈 + SL1 全清止損。

用 BINANCE_FUTURES 切換現貨／合約：
- 現貨（只做多）：倉位拆成數份，每份一張 OCO（止盈腿在各目標價、止損腿都在 SL1）。
- 合約（USDT-M，目前做多，預留做空）：N 張 reduce-only 止盈 + 1 張 STOP_MARKET
  closePosition 止損，碰 SL1 直接把整個倉位平掉。槓桿/保證金模式由 LEVERAGE/MARGIN_TYPE 設定。

策略（靜態）：進場時就把止盈/止損一次掛好；之後交易所自己執行，程式只負責對帳收單與保本。

⚠️ 預設連 testnet（假錢）。合約 testnet 與現貨 testnet 是不同網站、不同金鑰。
   現貨：testnet.binance.vision；合約：testnet.binancefuture.com
"""

import asyncio
import os
import time
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
# 對帳/收單迴圈的檢查間隔（秒）
MONITOR_INTERVAL = float(_get("TRADE_MONITOR_INTERVAL", "15"))
# 是否在第一個止盈成交後，把剩餘止損移到保本（進場價）
BREAKEVEN_AFTER_TP1 = _get("BREAKEVEN_AFTER_TP1", "1") == "1"
# 限價買單超過幾分鐘未成交就撤單、釋放持倉額度（0=永不超時，一直等成交）
ENTRY_TIMEOUT_MIN = float(_get("ENTRY_TIMEOUT_MIN", "30"))
ENTRY_TIMEOUT_SEC = ENTRY_TIMEOUT_MIN * 60
# 合約模式（USDT-M 永續）：1=合約 0=現貨
FUTURES = _get("BINANCE_FUTURES", "0") == "1"
LEVERAGE = int(_get("LEVERAGE", "1"))
MARGIN_TYPE = _get("MARGIN_TYPE", "ISOLATED").upper()
TRADE_SIDE = _get("TRADE_SIDE", "LONG").upper()  # 預留做空；目前只用 LONG


def _sides() -> tuple[str, str]:
    """回傳 (進場方向, 平倉方向)。LONG=BUY/SELL；SHORT=SELL/BUY（合約才支援）。"""
    if TRADE_SIDE == "SHORT":
        return "SELL", "BUY"
    return "BUY", "SELL"

_client: Client | None = None
_filters_cache: dict = {}
# 進場鎖：序列化「檢查額度→下單→建檔」，避免多訊號同時湧入時超開過 MAX_OPEN_TRADES
_entry_lock = asyncio.Lock()


def init() -> None:
    global _client
    if not (API_KEY and API_SECRET):
        raise SystemExit("交易已啟用但缺少 BINANCE_API_KEY / BINANCE_API_SECRET")
    _client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    trades.init()
    net = "TESTNET 測試網" if TESTNET else "⚠️ 正式網（真錢）"
    market = f"合約 {LEVERAGE}x {MARGIN_TYPE} {TRADE_SIDE}" if FUTURES else "現貨 LONG"
    print(f"[trader] 幣安自動交易啟用：{market} | {net} | 每筆 {TRADE_USDT} USDT | "
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
    if FUTURES:
        info = await _api(_client.futures_exchange_info)
        sym = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    else:
        sym = await _api(_client.get_symbol_info, symbol=symbol)
    if not sym:
        return None
    f = {x["filterType"]: x for x in sym["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    # 現貨用 minNotional、合約用 notional
    min_notional = notional.get("minNotional") or notional.get("notional") or "0"
    parsed = {
        "step": f["LOT_SIZE"]["stepSize"],
        "tick": f["PRICE_FILTER"]["tickSize"],
        "min_qty": Decimal(f["LOT_SIZE"]["minQty"]),
        "min_notional": Decimal(min_notional),
        "base": sym["baseAsset"],
        "quote": sym["quoteAsset"],
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

    # 進場鎖：序列化額度檢查與下單，避免多訊號並發時超開
    async with _entry_lock:
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

        # 預檢：現貨每張 OCO 都是真實賣單，分批後最小一份也要滿足最小金額。
        # 合約的止盈是 reduce-only、止損是 closePosition（平倉單），幣安不做此檢查，故略過。
        if not FUTURES:
            smallest = Decimal(str(min(TP_RATIOS))) / Decimal(str(sum(TP_RATIOS)))
            if qty * price * smallest < filt["min_notional"]:
                print(f"[trader] {symbol} 分批後最小一份 < 最小金額 {filt['min_notional']}，"
                      f"請調高 TRADE_USDT，略過")
                return

        order = await _open_entry(symbol, qty, price, filt)
        buy_id = order["orderId"]
        tid = trades.add(symbol=symbol, entry=float(price), qty=float(qty),
                         buy_order_id=buy_id, signal=signal)
        print(f"[trader] {symbol} 限價{('合約' if FUTURES else '')}進場單已掛 @ {price}"
              f"（qty={qty}，trade#{tid}），等待成交…")
        asyncio.create_task(_watch_and_protect(tid, order.get("status", "")))


async def _open_entry(symbol: str, qty: Decimal, price: Decimal, filt: dict) -> dict:
    """下進場限價單；合約會先設好槓桿與保證金模式。"""
    entry_side, _ = _sides()
    if FUTURES:
        await _api(_client.futures_change_leverage, symbol=symbol, leverage=LEVERAGE)
        try:
            await _api(_client.futures_change_margin_type, symbol=symbol, marginType=MARGIN_TYPE)
        except BinanceAPIException as e:
            if e.code != -4046:  # -4046 = 保證金模式無需變更（已是該模式）
                raise
        return await _api(_client.futures_create_order, symbol=symbol, side=entry_side,
                          type="LIMIT", timeInForce="GTC",
                          quantity=_qstr(float(qty), filt["step"]),
                          price=_qstr(float(price), filt["tick"]))
    # 現貨（只做多）
    return await _api(_client.order_limit_buy, symbol=symbol,
                      quantity=_qstr(float(qty), filt["step"]),
                      price=_qstr(float(price), filt["tick"]))


async def _get_order(symbol: str, order_id: int) -> dict:
    if FUTURES:
        return await _api(_client.futures_get_order, symbol=symbol, orderId=order_id)
    return await _api(_client.get_order, symbol=symbol, orderId=order_id)


async def _cancel_order(symbol: str, order_id: int) -> None:
    if FUTURES:
        await _api(_client.futures_cancel_order, symbol=symbol, orderId=order_id)
    else:
        await _api(_client.cancel_order, symbol=symbol, orderId=order_id)


async def _watch_and_protect(tid: int, initial_status: str = "") -> None:
    """等買單成交（已成交就跳過輪詢），成交後掛 OCO 止盈止損格。"""
    try:
        trade = trades.get(tid)
        symbol = trade["symbol"]
        order_id = trade["buy_order_id"]

        if initial_status != "FILLED":
            deadline = time.monotonic() + ENTRY_TIMEOUT_SEC if ENTRY_TIMEOUT_SEC > 0 else None
            # 剛下單時 testnet 可能因複寫延遲回 -2013（其實單子在），容忍重試
            not_found = 0
            await asyncio.sleep(1.0)
            while True:
                try:
                    order = await _get_order(symbol, order_id)
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
                if deadline and time.monotonic() >= deadline:
                    # 超時：撤掉買單，釋放持倉額度；若已部分成交則保護已成交的部分
                    if await _handle_entry_timeout(tid, symbol, order_id, order):
                        trade = trades.get(tid)  # 用更新後的成交量重新取 trade
                        break
                    return
                await asyncio.sleep(POLL_INTERVAL)

        await _place_protection(trade)
        trades.set_status(tid, "ACTIVE")
        print(f"[trader] {symbol} 已成交，止盈止損掛好，trade#{tid} → ACTIVE")
    except BinanceAPIException as e:
        print(f"[trader] trade#{tid} 掛保護單失敗：{e.status_code} {e.message}")
    except Exception as e:
        print(f"[trader] trade#{tid} 監控失敗：{e}")


async def _handle_entry_timeout(tid: int, symbol: str, order_id: int, order: dict) -> bool:
    """限價買單超時：撤單釋放額度。回傳是否「有可觀的部分成交、需改掛 OCO 保護」。"""
    try:
        await _cancel_order(symbol, order_id)
    except BinanceAPIException as e:
        if e.code != -2011:  # -2011 = 訂單已不存在（剛好成交/已撤），忽略
            raise

    filt = await _get_filters(symbol)
    executed = _quantize(float(order.get("executedQty", 0) or 0), filt["step"])
    price = Decimal(str(trades.get(tid)["entry"]))

    if executed >= filt["min_qty"] and executed * price >= filt["min_notional"]:
        # 已部分成交且量足夠拆單 → 用實際成交量保護
        trades.set_qty(tid, float(executed))
        print(f"[trader] {symbol} 進場單超時，已部分成交 {executed}，改用實際量掛保護單（trade#{tid}）")
        return True

    trades.set_status(tid, "CANCELED")
    mins = ENTRY_TIMEOUT_MIN
    print(f"[trader] {symbol} 買單超過 {mins:g} 分鐘未成交，已撤單，trade#{tid} 取消（釋放額度）")
    return False


def _split_portions(qty_total: Decimal, n: int, step: str) -> list[Decimal]:
    """把總量按 TP_RATIOS 拆成 n 份，最後一份吃剩餘避免 dust。"""
    ratios = TP_RATIOS[:n]
    total = Decimal(str(sum(ratios)))
    step_d = Decimal(str(step))
    portions, allocated = [], Decimal("0")
    for i in range(n):
        if i < n - 1:
            q = _quantize(float(qty_total * Decimal(str(ratios[i])) / total), step)
        else:
            q = ((qty_total - allocated) // step_d) * step_d
        allocated += q
        portions.append(q)
    return portions


async def _place_protection(trade: dict) -> None:
    """掛止盈止損：合約走 reduce-only/closePosition，現貨走 OCO 格。"""
    if FUTURES:
        await _place_protection_futures(trade)
    else:
        await _place_oco_grid(trade)


async def _place_protection_futures(trade: dict) -> None:
    """合約：掛 N 張 reduce-only 止盈 + 1 張 closePosition 止損（碰 SL1 全平）。"""
    symbol = trade["symbol"]
    signal = trade["signal"]
    filt = await _get_filters(symbol)
    targets = signal["targets"]
    _, close_side = _sides()
    sl1 = _quantize(signal["stops"][0]["price"], filt["tick"])

    n = min(len(TP_RATIOS), len(targets))
    portions = _split_portions(Decimal(str(trade["qty"])), n, filt["step"])

    tp_orders = []
    for i in range(n):
        qty_i = portions[i]
        if qty_i <= 0:
            continue
        tp_price = _quantize(targets[i]["price"], filt["tick"])
        resp = await _api(
            _client.futures_create_order, symbol=symbol, side=close_side,
            type="TAKE_PROFIT_MARKET", stopPrice=format(tp_price, "f"),
            quantity=format(qty_i, "f"), reduceOnly="true",
        )
        tp_orders.append({"order_id": resp["orderId"], "qty": float(qty_i),
                          "tp": float(tp_price), "level": i + 1})
        print(f"[trader]   TP{i + 1}: {close_side} {qty_i} @ {tp_price}（reduceOnly）")

    # 止損：一張 STOP_MARKET closePosition，碰 SL1 直接把整個倉位平掉
    sl_resp = await _api(
        _client.futures_create_order, symbol=symbol, side=close_side,
        type="STOP_MARKET", stopPrice=format(sl1, "f"), closePosition="true",
    )
    print(f"[trader]   SL: {close_side} 全平 @ {sl1}（closePosition）")
    trades.set_oco(trade["id"], {"mode": "futures", "sl_order_id": sl_resp["orderId"],
                                 "sl_price": float(sl1), "tp_orders": tp_orders})


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

    placed = []
    for i in range(n):
        qty_i = portions[i]
        if qty_i <= 0:
            continue
        tp_price = _quantize(targets[i]["price"], filt["tick"])
        # 新版 OCO endpoint（orderList/oco）：SELL 時 above=止盈(LIMIT_MAKER)、below=止損(STOP_LOSS_LIMIT)
        resp = await _api(
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
        placed.append({
            "list_id": resp["orderListId"],
            "qty": float(qty_i),
            "tp": float(tp_price),
            "level": i + 1,
        })
        print(f"[trader]   OCO{i + 1}: 賣 {qty_i} @ TP {tp_price} / SL {sl_trigger}")

    trades.set_oco(trade["id"], placed)


async def resume() -> None:
    """程式重啟時，把仍在等成交的買單重新接上監控。"""
    for t in trades.list_status("PENDING_BUY"):
        print(f"[trader] 回復未完成交易 trade#{t['id']} {t['symbol']}，繼續等待成交…")
        asyncio.create_task(_watch_and_protect(t["id"]))


def start_monitor() -> None:
    """啟動背景對帳迴圈：自動收單（#1）+ TP1 後移動停損到保本（#2）。"""
    asyncio.create_task(_monitor_loop())
    print(f"[trader] 對帳迴圈啟動（每 {MONITOR_INTERVAL:g} 秒）"
          f"{'，TP1 後移動停損到保本' if BREAKEVEN_AFTER_TP1 else ''}")


async def _monitor_loop() -> None:
    while True:
        try:
            for trade in trades.list_status("ACTIVE"):
                await _reconcile(trade)
        except Exception as e:
            print(f"[trader] 對帳迴圈錯誤：{e}")
        await asyncio.sleep(MONITOR_INTERVAL)


async def _reconcile(trade: dict) -> None:
    if FUTURES:
        await _reconcile_futures(trade)
    else:
        await _reconcile_spot(trade)


async def _reconcile_futures(trade: dict) -> None:
    """合約：倉位歸零就收單；倉位減少（有止盈成交）就把止損移到保本。"""
    symbol = trade["symbol"]
    info = trade.get("oco_orders") or {}
    pos = await _api(_client.futures_position_information, symbol=symbol)
    amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0

    # #1 自動收單：倉位歸零 = 已全平 → 撤掉殘留止盈止損單、標記 CLOSED
    if amt == 0:
        await _api(_client.futures_cancel_all_open_orders, symbol=symbol)
        trades.set_status(trade["id"], "CLOSED")
        print(f"[trader] trade#{trade['id']} {symbol} 倉位已平 → CLOSED（釋放持倉額度）")
        return

    # #2 移動止損到保本：倉位比原始量少 = 有止盈成交（止損會一次全平，不會只少一部分）
    if (BREAKEVEN_AFTER_TP1 and not trade.get("sl_moved")
            and amt < float(trade["qty"]) * 0.999):
        await _move_sl_breakeven_futures(trade, info)


async def _move_sl_breakeven_futures(trade: dict, info: dict) -> None:
    """合約：撤掉舊止損、在進場價重掛 closePosition 止損（保本）。"""
    symbol = trade["symbol"]
    filt = await _get_filters(symbol)
    entry = Decimal(str(trade["entry"]))
    _, close_side = _sides()

    ticker = await _api(_client.futures_symbol_ticker, symbol=symbol)
    price = Decimal(str(ticker["price"]))
    if entry >= price:
        return  # 已回落到進場價附近，設保本會讓止損高於市價，維持原止損

    be = _quantize(float(entry), filt["tick"])
    old_sl = info.get("sl_order_id")
    if old_sl:
        try:
            await _api(_client.futures_cancel_order, symbol=symbol, orderId=old_sl)
        except BinanceAPIException as e:
            if e.code != -2011:
                raise
    resp = await _api(_client.futures_create_order, symbol=symbol, side=close_side,
                      type="STOP_MARKET", stopPrice=format(be, "f"), closePosition="true")
    info["sl_order_id"] = resp["orderId"]
    info["sl_price"] = float(be)
    trades.update_oco(trade["id"], info, sl_moved=True)
    print(f"[trader] trade#{trade['id']} {symbol} 止盈已觸發，止損移到保本 {be}")


async def _reconcile_spot(trade: dict) -> None:
    """檢查一筆 ACTIVE 交易：全平就收單；部分止盈就把剩餘止損移到保本。"""
    symbol = trade["symbol"]
    legs = trade.get("oco_orders") or []
    open_orders = await _api(_client.get_open_orders, symbol=symbol)

    if not legs:
        # 舊資料沒記 OCO（功能上線前的交易）：沒有未結單就視為已結束
        if not open_orders:
            trades.set_status(trade["id"], "CLOSED")
            print(f"[trader] trade#{trade['id']} {symbol} 已結束 → CLOSED")
        return

    open_list_ids = {o["orderListId"] for o in open_orders}
    still_open = [lg for lg in legs if lg["list_id"] in open_list_ids]

    # #1 自動收單：全部 OCO 都不在了 = 整筆平倉完成
    if not still_open:
        trades.set_status(trade["id"], "CLOSED")
        print(f"[trader] trade#{trade['id']} {symbol} 全部平倉 → CLOSED（釋放持倉額度）")
        return

    # #2 移動停損到保本：有些已平、有些還在 → 已平者必為止盈（止損會一次掃全部）
    if (BREAKEVEN_AFTER_TP1 and not trade.get("sl_moved")
            and len(still_open) < len(legs)):
        await _move_sl_to_breakeven(trade, still_open, open_orders)


async def _move_sl_to_breakeven(trade: dict, still_open: list, open_orders: list) -> None:
    """把仍掛著的 OCO 撤掉重掛，止盈不變、止損改到進場價（保本）。"""
    symbol = trade["symbol"]
    filt = await _get_filters(symbol)
    entry = Decimal(str(trade["entry"]))

    ticker = await _api(_client.get_symbol_ticker, symbol=symbol)
    price = Decimal(ticker["price"])
    if entry >= price:
        return  # 已回落到進場價附近，設保本會讓止損高於市價（OCO 不合法），維持原止損

    be_trigger = _quantize(float(entry), filt["tick"])
    be_limit = _quantize(float(entry * Decimal(str(1 - SL_LIMIT_BUFFER_PCT / 100))), filt["tick"])
    order_by_list = {}
    for o in open_orders:
        order_by_list.setdefault(o["orderListId"], o)

    still_ids = {s["list_id"] for s in still_open}
    legs = [dict(lg) for lg in trade["oco_orders"]]
    for lg in legs:
        if lg["list_id"] not in still_ids:
            continue
        o = order_by_list.get(lg["list_id"])
        if not o:
            continue
        # 撤掉舊 OCO（撤一腿即連帶取消整組），再用相同止盈、保本止損重掛
        await _api(_client.cancel_order, symbol=symbol, orderId=o["orderId"])
        resp = await _api(
            _client.create_oco_order, symbol=symbol, side="SELL",
            quantity=format(_quantize(lg["qty"], filt["step"]), "f"),
            aboveType="LIMIT_MAKER", abovePrice=format(_quantize(lg["tp"], filt["tick"]), "f"),
            belowType="STOP_LOSS_LIMIT",
            belowStopPrice=format(be_trigger, "f"), belowPrice=format(be_limit, "f"),
            belowTimeInForce="GTC",
        )
        lg["list_id"] = resp["orderListId"]

    trades.update_oco(trade["id"], legs, sl_moved=True)
    print(f"[trader] trade#{trade['id']} {symbol} 止盈已觸發，剩餘 {len(still_open)} 組止損移到保本 {be_trigger}")
