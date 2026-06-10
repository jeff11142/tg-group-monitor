"""幣安自動化交易：依訊號限價進場，成交後掛多段止盈 + 雙軌半倉止損。

用 BINANCE_FUTURES 切換現貨／合約：
- 現貨（只做多）：倉位拆成數份，每份一張 OCO（止盈腿在各目標價、止損腿都在 SL1）。
- 合約（USDT-M，目前做多，預留做空）：N 張 reduce-only 止盈（30/30/20/20）＋ 兩張
  reduce-only 半倉止損（上軌 SL1、下軌 SL2）。每段 TP 成交後撤掉兩道止損、在階梯新階重掛
  （各取當下剩餘半倉），達成逐段鎖利。槓桿/保證金模式由 LEVERAGE/MARGIN_TYPE 設定。

策略：進場一次掛好止盈與雙軌止損；之後由 WebSocket（User Data Stream）即時反應訂單成交
（TP 成交→止損上移、倉位歸零→收單），另有慢速對帳迴圈當保險絲兜底/補網/重啟回復。

⚠️ 預設連 testnet（假錢）。合約 testnet 與現貨 testnet 是不同網站、不同金鑰。
   現貨：testnet.binance.vision；合約：testnet.binancefuture.com
"""

import asyncio
import os
import time
from decimal import ROUND_DOWN, Decimal

from binance import AsyncClient, BinanceSocketManager
from binance.client import Client
from binance.exceptions import BinanceAPIException

import trades


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


TESTNET = _get("BINANCE_TESTNET", "1") == "1"


def _api_keys(testnet: bool) -> tuple[str, str]:
    """依網路取對應金鑰。測試網可退回舊的 BINANCE_API_KEY/SECRET（相容舊 .env）；
    正式網一定要用 BINANCE_MAINNET_*（不退回，免得誤用測試網金鑰打真錢）。"""
    if testnet:
        return (_get("BINANCE_TESTNET_API_KEY") or _get("BINANCE_API_KEY"),
                _get("BINANCE_TESTNET_API_SECRET") or _get("BINANCE_API_SECRET"))
    return (_get("BINANCE_MAINNET_API_KEY"), _get("BINANCE_MAINNET_API_SECRET"))


API_KEY, API_SECRET = _api_keys(TESTNET)
TRADE_USDT = float(_get("TRADE_USDT", "50"))
# 合約固定本金（USDT）：>0 時用「名目 = 本金 × 槓桿」算倉位（保證金一致優先）。0=沿用舊邏輯。
MARGIN_USDT = float(_get("MARGIN_USDT", "0"))
# 自動最小金額：1=每筆依交易對自動算出「能成功下單＋拆得出分批」的最小金額並用它下單
AUTO_MIN_AMOUNT = _get("AUTO_MIN_AMOUNT", "0") == "1"
# 自動最小金額模式：先把幣安最小可下量乘上這個倍數當「投入保證金本金」，再乘槓桿成下單名目。
MIN_AMOUNT_MULT = float(_get("MIN_AMOUNT_MULT", "10"))
MAX_OPEN_TRADES = int(_get("MAX_OPEN_TRADES", "5"))
TP_RATIOS = [float(x) for x in _get("TP_RATIOS", "30,30,20,20").split(",") if x.strip()]
SL_LIMIT_BUFFER_PCT = float(_get("SL_LIMIT_BUFFER_PCT", "0.3"))
POLL_INTERVAL = float(_get("ENTRY_POLL_INTERVAL", "5"))
# 賣單預留的手續費緩衝（%）：用買進數量拆單時扣掉，避免「賣超」實得數量
FEE_BUFFER_PCT = float(_get("SELL_FEE_BUFFER_PCT", "0.1"))
# 失效保險絲/補網迴圈間隔（秒）：即時反應已交給 WebSocket，這支只當慢速兜底（漏訊息/斷線/重啟回復）
MONITOR_INTERVAL = float(_get("TRADE_MONITOR_INTERVAL", "120"))
# 是否在止盈成交後啟動動態止損（TP1→保本、之後逐段鎖利）
BREAKEVEN_AFTER_TP1 = _get("BREAKEVEN_AFTER_TP1", "1") == "1"
# 淨保本：移到保本時把止損設在「進場價 ×（1 ± 此手續費%）」，涵蓋來回手續費，移動後即使被掃也不虧
BREAKEVEN_FEE_PCT = float(_get("BREAKEVEN_FEE_PCT", "0.1"))
# 合約止損：SL1 = min(訊號SL1 距離, SL1_PCT 上限%)；SL2 = SL1 × SL2_MULT。
# 例：訊號SL1 -3% → 上軌 -3%、下軌 -6%；訊號SL1 -8%（超上限）→ 上軌 -5%、下軌 -10%。
# 每中一段 TP 雙軌整組往上爬一階（保本→TP1→…）。做多往下、做空往上。
SL1_PCT = float(_get("SL1_PCT", "5"))    # SL1 上限%（訊號更小就用訊號的）
SL2_MULT = float(_get("SL2_MULT", "2"))  # SL2 = 生效 SL1 × 此倍數；0 = 不掛 SL2（單一止損守全倉）
# 下單止損倍數：僅「現貨」OCO 仍沿用（合約已改用上方固定 SL1_PCT/SL2_PCT）。
# 實際掛的止損 = 進場 + 此倍數 ×（訊號止損1 − 進場）。轉發訊息仍用訊號原始止損，不受此影響。
SL_MULTIPLIER = float(_get("SL_MULTIPLIER", "2"))
# 限價買單超過幾分鐘未成交就撤單、釋放持倉額度（0=永不超時，一直等成交）
ENTRY_TIMEOUT_MIN = float(_get("ENTRY_TIMEOUT_MIN", "30"))
# 合約模式（USDT-M 永續）：1=合約 0=現貨
FUTURES = _get("BINANCE_FUTURES", "0") == "1"
LEVERAGE = int(_get("LEVERAGE", "1"))
MARGIN_TYPE = _get("MARGIN_TYPE", "ISOLATED").upper()
TRADE_SIDE = _get("TRADE_SIDE", "LONG").upper()  # 預留做空；目前只用 LONG
# 止損保險絲：對帳時若價格已穿過止損價但倉位還在（交易所條件單失靈）就主動市價平倉
SL_FAILSAFE = _get("SL_FAILSAFE", "1") == "1"
# 止盈保險絲：對帳時若價格已穿過某段止盈但該段還掛著沒成交，就逐段市價補平那一段
TP_FAILSAFE = _get("TP_FAILSAFE", "1") == "1"


def _sides() -> tuple[str, str]:
    """回傳 (進場方向, 平倉方向)。LONG=BUY/SELL；SHORT=SELL/BUY（合約才支援）。"""
    if TRADE_SIDE == "SHORT":
        return "SELL", "BUY"
    return "BUY", "SELL"


def _effective_stop(signal: dict) -> float:
    """實際下單用的止損價：把訊號止損1 相對進場的距離放大 SL_MULTIPLIER 倍。
    = 進場 + 倍數 ×（訊號止損1 − 進場）。轉發訊息仍用訊號原始止損，不走這裡。"""
    entry = float(signal["entry"])
    sl1 = float(signal["stops"][0]["price"])
    return entry + SL_MULTIPLIER * (sl1 - entry)

_client: Client | None = None
_filters_cache: dict = {}
# 進場鎖：序列化「檢查額度→下單→建檔」，避免多訊號同時湧入時超開過 MAX_OPEN_TRADES
_entry_lock = asyncio.Lock()
# 保留背景任務參考，避免事件迴圈只持弱參考導致任務被 GC（Python asyncio 官方警告）
_bg_tasks: set = set()
# User Data Stream（WebSocket）用的非同步 client；與同步 _client 並存
_async_client: AsyncClient | None = None
# WebSocket 背景任務參考（切換網路時要 cancel 後重啟）
_ws_task: "asyncio.Task | None" = None
# 止損上移鎖：WS 即時事件與慢速保險絲都可能觸發，序列化避免重複撤單/掛單
_sl_update_lock = asyncio.Lock()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def init() -> None:
    global _client
    if not (API_KEY and API_SECRET):
        net = "測試網" if TESTNET else "正式網"
        raise SystemExit(f"交易已啟用但缺少{net} API 金鑰（檢查 .env 的 BINANCE_*_API_KEY/SECRET）")
    _client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    trades.init()
    net = "TESTNET 測試網" if TESTNET else "⚠️ 正式網（真錢）"
    market = f"合約 {LEVERAGE}x {MARGIN_TYPE} {TRADE_SIDE}" if FUTURES else "現貨 LONG"
    if AUTO_MIN_AMOUNT:
        amount_desc = "依交易對自動取最小"
    elif FUTURES and MARGIN_USDT > 0:
        amount_desc = f"固定本金 {MARGIN_USDT:g} × {LEVERAGE}x = 名目 {MARGIN_USDT * LEVERAGE:g} USDT"
    else:
        amount_desc = f"固定名目 {TRADE_USDT:g} USDT"
    print(f"[trader] 幣安自動交易啟用：{market} | {net} | 每筆 {amount_desc} | "
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


def _oid(resp: dict):
    """合約條件單（TP/SL）回 algoId、一般單回 orderId，取存在的那個。"""
    return resp.get("orderId") or resp.get("algoId")


def _min_amount(filt: dict, price: float, n: int) -> float:
    """算出某交易對「能成功下單且拆得出 n 段分批」的最小金額（USDT，含緩衝）。

    需同時滿足：
    - 進場名目 ≥ minNotional
    - 分批後最小一份 ≥ minQty（每段才下得了單）
    - 現貨額外要求：最小一份名目也 ≥ minNotional（OCO 每腿都是真實賣單）
    """
    ratios = TP_RATIOS[:n]
    smallest = min(ratios) / sum(ratios)
    min_notional = float(filt["min_notional"])
    min_qty = float(filt["min_qty"])
    step = float(filt["step"])
    # 每段至少 minQty：總量 ≥ (minQty + 一個 step 緩衝) / 最小比例
    notional_for_qty = (min_qty + step) / smallest * price
    if FUTURES:
        base = max(min_notional, notional_for_qty)
    else:
        base = max(min_notional / smallest, notional_for_qty)
    return base * 1.05  # 5% 緩衝，避免取整後低於門檻


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
        n = min(len(TP_RATIOS), len(targets))
        if AUTO_MIN_AMOUNT:
            # 名目 = 幣安最小可下量 × 倍數 × 槓桿。最小量×倍數＝實際投入的保證金本金，
            # 再乘槓桿才是下單名目。例：min=5.25、×10＝保證金 52.5、×10x → 名目 525 USDT。
            base = _min_amount(filt, float(price), n)
            margin = base * MIN_AMOUNT_MULT
            amount = margin * LEVERAGE
            print(f"[trader] {symbol} 自動最小金額：{base:.2f} × {MIN_AMOUNT_MULT:g}"
                  f" = 保證金 {margin:.2f} USDT × {LEVERAGE}x → 名目 {amount:.2f} USDT"
                  f"（minNotional={filt['min_notional']}, minQty={filt['min_qty']}）")
        elif FUTURES and MARGIN_USDT > 0:
            # 保證金一致優先：名目 = 固定本金 × 槓桿，每筆佔用保證金固定 = MARGIN_USDT
            amount = MARGIN_USDT * LEVERAGE
            print(f"[trader] {symbol} 固定本金 {MARGIN_USDT:g} USDT × {LEVERAGE}x"
                  f" → 名目 {amount:.2f} USDT")
        else:
            amount = TRADE_USDT
        qty = _quantize(amount / float(price), filt["step"])
        if qty < filt["min_qty"] or qty * price < filt["min_notional"]:
            print(f"[trader] {symbol} 下單量太小（qty={qty}），請調高 TRADE_USDT，略過")
            return

        # 預檢：現貨每張 OCO 都是真實賣單，分批後最小一份也要滿足最小金額。
        # 合約的止盈/止損都是 reduce-only 市價條件單，幣安不做此最小名目檢查，故略過。
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
        _spawn(_watch_and_protect(tid, order.get("status", "")))


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
            # 進場掛單存活秒數（即時讀 ENTRY_TIMEOUT_MIN，Bot 改了下一筆就生效）；0=永不超時
            timeout_sec = ENTRY_TIMEOUT_MIN * 60
            deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None
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

        if await _place_protection(trade):
            trades.set_status(tid, "ACTIVE")
            print(f"[trader] {symbol} 已成交，止盈止損掛好，trade#{tid} → ACTIVE")
    except BinanceAPIException as e:
        print(f"[trader] trade#{tid} 掛保護單失敗：{e.status_code} {e.message}")
    except Exception as e:
        print(f"[trader] trade#{tid} 監控失敗：{e!r}")


async def _handle_entry_timeout(tid: int, symbol: str, order_id: int, order: dict) -> bool:
    """限價買單超時：撤單釋放額度。回傳是否「已成交（全部或可觀部分）、需掛保護單」。"""
    # 撤單前先重查最新狀態，避免「剛好在超時瞬間成交」卻用舊資料誤判成未成交
    try:
        order = await _get_order(symbol, order_id)
    except BinanceAPIException:
        pass  # 查不到就沿用傳入的舊 order

    if order.get("status") == "FILLED":
        print(f"[trader] {symbol} 超時前其實已全部成交，照常掛保護單（trade#{tid}）")
        return True  # trade qty 即原始下單量，直接掛完整保護

    # 仍未完全成交 → 撤掉剩餘
    try:
        await _cancel_order(symbol, order_id)
    except BinanceAPIException as e:
        if e.code != -2011:  # -2011 = 訂單已不存在（剛好成交/已撤），忽略
            raise

    # 撤單後再查一次最終成交量（撤單瞬間可能又成交一部分）
    try:
        order = await _get_order(symbol, order_id)
    except BinanceAPIException:
        pass

    filt = await _get_filters(symbol)
    if order.get("status") == "FILLED":  # 撤單那瞬間剛好全部成交
        print(f"[trader] {symbol} 撤單前剛好全部成交，照常掛保護單（trade#{tid}）")
        return True
    executed = _quantize(float(order.get("executedQty", 0) or 0), filt["step"])
    price = Decimal(str(trades.get(tid)["entry"]))

    if executed >= filt["min_qty"] and executed * price >= filt["min_notional"]:
        # 已部分成交且量足夠拆單 → 用實際成交量保護
        trades.set_qty(tid, float(executed))
        print(f"[trader] {symbol} 進場單超時，已部分成交 {executed}，改用實際量掛保護單（trade#{tid}）")
        return True

    trades.set_status(tid, "CANCELED")
    print(f"[trader] {symbol} 買單超過 {ENTRY_TIMEOUT_MIN:g} 分鐘未成交，已撤單，trade#{tid} 取消（釋放額度）")
    return False


def _adaptive_portions(qty_total: Decimal, step: str, min_qty,
                       n_targets: int) -> list[Decimal]:
    """保證金一致優先：依倉位大小自動決定止盈段數。

    拆得出幾段就掛幾段，對應「最近的 N 個目標」（portions[0]=TP1）；倉位太小時自動降級
    （最少 1 段＝全押 TP1）。每段都保證 ≥ minQty，零頭補到最近端（TP1）避免 dust。
    """
    step_d = Decimal(str(step))
    min_q = Decimal(str(min_qty))
    desired = max(min(len(TP_RATIOS), n_targets), 1)
    # 由多到少試出可行段數 k：用前 k 個比例，最小一份取整後仍 ≥ minQty 才成立
    k = desired
    while k > 1:
        ratios_k = TP_RATIOS[:k]
        smallest = _quantize(
            float(qty_total * Decimal(str(min(ratios_k))) / Decimal(str(sum(ratios_k)))), step)
        if smallest >= min_q:
            break
        k -= 1
    # 遠端各取比例（往下取整），最近端 TP1 吃剩餘吸收零頭
    ratios_k = TP_RATIOS[:k]
    total = Decimal(str(sum(ratios_k)))
    portions = [Decimal("0")] * k
    allocated = Decimal("0")
    for i in range(k - 1, 0, -1):
        q = _quantize(float(qty_total * Decimal(str(ratios_k[i])) / total), step)
        portions[i] = q
        allocated += q
    portions[0] = ((qty_total - allocated) // step_d) * step_d
    return portions


async def _place_protection(trade: dict) -> bool:
    """掛止盈止損。回傳 True=已掛好(該設 ACTIVE)；False=進場即穿止損已直接平倉(已 CLOSED)。"""
    if FUTURES:
        return await _place_protection_futures(trade)
    await _place_oco_grid(trade)
    return True


async def _market_close_qty(symbol: str, close_side: str, qty: Decimal) -> None:
    """市價 reduceOnly 平掉指定數量（忽略 -2022 倉位不足）。"""
    try:
        await _api(_client.futures_create_order, symbol=symbol, side=close_side,
                   type="MARKET", quantity=format(qty, "f"), reduceOnly="true")
    except BinanceAPIException as e:
        if e.code != -2022:  # -2022 reduceOnly 被拒（倉位已不足）
            raise


async def _place_protection_futures(trade: dict) -> bool:
    """合約：掛雙軌半倉止損（SL1 上軌 / SL2 下軌）＋ N 段止盈（30/30/20/20）。"""
    symbol = trade["symbol"]
    signal = trade["signal"]
    filt = await _get_filters(symbol)
    targets = signal["targets"]
    _, close_side = _sides()

    # 先確認真的有持倉才掛保護（重啟回復/延遲時，倉位可能已被平掉或從未開成）
    pos = await _api(_client.futures_position_information, symbol=symbol)
    amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0
    if amt == 0:
        print(f"[trader] {symbol} 無持倉可保護（可能已平倉），trade#{trade['id']} → CLOSED")
        trades.set_status(trade["id"], "CLOSED")
        return False

    # 冪等保護：先撤掉此交易對殘留的條件單，避免重啟回復重跑到這時 SL/TP 被重複掛成兩套。
    # 同一交易對同時只允許一筆（has_open_symbol），所以殘留的一定是本筆的舊單，可安全清掉重掛。
    await _cancel_all_futures_orders(symbol)

    # 止損：初始（tier 0）掛 rung[1]=SL1。SL2_MULT>0 → 雙軌（上軌 SL1、下軌 SL2 各半倉）；
    # SL2_MULT=0 → 單軌守全倉（捨棄 SL2）。
    rungs = _sl_ladder(signal)
    sl_ids, sl_prices = await _place_dual_sls(symbol, rungs[1], rungs[0], amt, filt,
                                              dual=SL2_MULT > 0)

    # 止盈：依倉位大小自動決定段數（保證金一致優先），對應最近的 N 個目標。
    # 某段若進場時已達 → 市價賣掉那段（直接落袋）。
    portions = _adaptive_portions(Decimal(str(trade["qty"])), filt["step"],
                                  filt["min_qty"], len(targets))
    tp_orders = []
    for i, qty_i in enumerate(portions):
        if qty_i <= 0:
            continue
        tp_price = _quantize(targets[i]["price"], filt["tick"])
        try:
            resp = await _api(
                _client.futures_create_order, symbol=symbol, side=close_side,
                type="TAKE_PROFIT_MARKET", stopPrice=format(tp_price, "f"),
                quantity=format(qty_i, "f"), reduceOnly="true",
            )
            tp_orders.append({"order_id": _oid(resp), "qty": float(qty_i),
                              "tp": float(tp_price), "level": i + 1})
            print(f"[trader]   TP{i + 1}: {close_side} {qty_i} @ {tp_price}（reduceOnly）")
        except BinanceAPIException as e:
            if e.code != -2021:
                raise
            print(f"[trader] ⚠️ {symbol} TP{i + 1} {tp_price} 進場時已達 → 市價賣出該段 {qty_i}")
            await _market_close_qty(symbol, close_side, qty_i)

    trades.set_oco(trade["id"], {"mode": "futures", "sl_orders": sl_ids,
                                 "sl_prices": sl_prices, "tp_orders": tp_orders})
    return True


async def _place_oco_grid(trade: dict) -> None:
    symbol = trade["symbol"]
    signal = trade["signal"]
    filt = await _get_filters(symbol)
    targets = signal["targets"]
    sl1 = _effective_stop(signal)
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
        _spawn(_watch_and_protect(t["id"]))


def start_monitor() -> None:
    """啟動背景保險絲迴圈（慢速兜底）：自動收單、止損補網、TP 後止損上移。"""
    _spawn(_monitor_loop())
    sl_desc = "，雙軌半倉止損逐段上移（階梯鎖利）" if BREAKEVEN_AFTER_TP1 else ""
    print(f"[trader] 保險絲對帳迴圈啟動（每 {MONITOR_INTERVAL:g} 秒）{sl_desc}")


async def start_user_stream() -> None:
    """啟動 User Data Stream（WebSocket）：訂單成交即時推播，取代高頻輪詢，避免 -1003 限流。"""
    global _ws_task
    if not FUTURES:
        return
    _ws_task = asyncio.create_task(_user_stream_loop())
    _bg_tasks.add(_ws_task)
    _ws_task.add_done_callback(_bg_tasks.discard)


def current_network() -> str:
    """目前網路的人話描述（給 log / Bot 顯示）。"""
    return "TESTNET 測試網" if TESTNET else "⚠️ 正式網（真錢）"


def mainnet_keys_ready() -> bool:
    """正式網金鑰是否已備妥（切到正式網前先確認，避免切進壞狀態）。"""
    k, s = _api_keys(False)
    return bool(k and s)


async def switch_network(to_testnet: bool) -> tuple[bool, str]:
    """切換測試/正式網：重建同步 client 與 WebSocket。**只在無未結倉時允許**。
    回傳 (是否成功, 訊息)。持久化（寫回 .env BINANCE_TESTNET）由呼叫端負責。"""
    global TESTNET, API_KEY, API_SECRET, _client, _ws_task
    if to_testnet == TESTNET:
        return False, f"目前已經是 {current_network()}，無需切換"
    n = trades.count_open()
    if n > 0:
        return False, (f"⚠️ 還有 {n} 筆未結倉，請先全部平倉再切換"
                       "（否則舊網路的倉位會失去自動管理）")
    key, secret = _api_keys(to_testnet)
    if not (key and secret):
        net = "測試網" if to_testnet else "正式網"
        return False, f"❌ 找不到{net}的 API 金鑰，請先在 .env 設好再切換"
    # 套用新網路 + 重建同步 client
    TESTNET, API_KEY, API_SECRET = to_testnet, key, secret
    _client = Client(API_KEY, API_SECRET, testnet=TESTNET)
    # 重啟 WebSocket（用新網路與金鑰重連）
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass
    if FUTURES:
        _ws_task = asyncio.create_task(_user_stream_loop())
        _bg_tasks.add(_ws_task)
        _ws_task.add_done_callback(_bg_tasks.discard)
    print(f"[trader] 已切換網路 → {current_network()}")
    return True, f"✅ 已切換到 {current_network()}"


async def _user_stream_loop() -> None:
    """連線 futures user socket，逐則處理；斷線指數退避重連。listenKey 保活由套件內部處理。"""
    global _async_client
    backoff = 1
    while True:
        try:
            _async_client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)
            bsm = BinanceSocketManager(_async_client)
            async with bsm.futures_user_socket() as stream:
                print("[trader] WebSocket 已連線，即時監聽訂單/倉位更新")
                backoff = 1
                while True:
                    msg = await stream.recv()
                    if not msg or msg.get("e") == "error":
                        print(f"[trader] WS 收到錯誤/空訊息，重連：{msg}")
                        break
                    await _on_user_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[trader] WS 中斷，{backoff}s 後重連：{e!r}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if _async_client is not None:
                try:
                    await _async_client.close_connection()
                except Exception:
                    pass
                _async_client = None


async def _on_user_event(msg: dict) -> None:
    """分流 User Data Stream 事件；目前只處理訂單成交（ORDER_TRADE_UPDATE）。"""
    try:
        if msg.get("e") == "ORDER_TRADE_UPDATE":
            await _on_order_filled(msg.get("o") or {})
    except Exception as e:
        print(f"[trader] WS 事件處理失敗：{e!r}")


def _find_active_trade_by_order(symbol: str, oid) -> dict | None:
    """用訂單 ID 找出這是哪一筆 ACTIVE 交易的止盈/止損單。"""
    for t in trades.list_status("ACTIVE"):
        if t["symbol"] != symbol:
            continue
        info = t.get("oco_orders") or {}
        ids = {x.get("order_id") for x in info.get("tp_orders") or []}
        ids.update(info.get("sl_orders") or [])
        if info.get("sl_order_id"):
            ids.add(info["sl_order_id"])
        if oid in ids:
            return t
    return None


async def _on_order_filled(o: dict) -> None:
    """我們掛的某張 TP/SL 成交了：倉位歸零就收單；否則（多半是 TP 成交）把雙軌止損上移。"""
    if o.get("X") != "FILLED":
        return
    symbol, oid = o.get("s"), o.get("i")
    trade = _find_active_trade_by_order(symbol, oid)
    if not trade:
        return  # 不是我們追蹤的單（或已處理）
    info = trade.get("oco_orders") or {}
    pos = await _api(_client.futures_position_information, symbol=symbol)
    amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0
    if amt == 0:
        await _cancel_all_futures_orders(symbol)
        trades.set_status(trade["id"], "CLOSED")
        print(f"[trader] trade#{trade['id']} {symbol} 倉位已平 → CLOSED（WS 即時）")
        return
    if BREAKEVEN_AFTER_TP1:
        await _update_dual_sl_futures(trade, info, amt)


async def _monitor_loop() -> None:
    while True:
        try:
            actives = trades.list_status("ACTIVE")
            # 一輪只抓一次帳戶級快照（全部持倉/條件單/現價），所有 ACTIVE 共用，
            # 避免逐筆逐 symbol 打 REST 把 IP 打到限流被 ban（-1003）。
            snap = await _account_snapshot() if (actives and FUTURES) else None
            for trade in actives:
                await _reconcile(trade, snap)
        except Exception as e:
            print(f"[trader] 對帳迴圈錯誤：{e}")
        await asyncio.sleep(MONITOR_INTERVAL)


async def _account_snapshot() -> dict:
    """一輪一次：抓全帳戶持倉、全條件單、全現價，建成查表供本輪所有交易共用。
    把「N 筆 × 每筆數個 REST」壓成固定 3 個請求，是避免 -1003 限流的關鍵。"""
    pos = await _api(_client.futures_position_information)
    cond = await _api(_client.futures_get_open_orders, conditional=True)
    tickers = await _api(_client.futures_symbol_ticker)
    return {
        "pos": {p["symbol"]: abs(float(p["positionAmt"])) for p in pos},
        "open_cond_ids": {(o.get("orderId") or o.get("algoId")) for o in cond},
        "prices": {t["symbol"]: float(t["price"]) for t in tickers},
    }


async def _reconcile(trade: dict, snap: dict | None = None) -> None:
    if FUTURES:
        await _reconcile_futures(trade, snap)
    else:
        await _reconcile_spot(trade)


async def _reconcile_futures(trade: dict, snap: dict | None = None) -> None:
    """合約：倉位歸零就收單；倉位減少（有止盈成交）就把止損移到保本。
    snap=帳戶級快照（對帳迴圈傳入，省 REST）；None 時退回逐 symbol 查詢（單筆測試用）。"""
    symbol = trade["symbol"]
    info = trade.get("oco_orders") or {}
    if snap is not None:
        amt = snap["pos"].get(symbol, 0.0)
    else:
        pos = await _api(_client.futures_position_information, symbol=symbol)
        amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0

    # #1 自動收單：倉位歸零 = 已全平 → 撤掉殘留止盈止損單、標記 CLOSED
    if amt == 0:
        await _cancel_all_futures_orders(symbol)
        trades.set_status(trade["id"], "CLOSED")
        print(f"[trader] trade#{trade['id']} {symbol} 倉位已平 → CLOSED（釋放持倉額度）")
        return

    # 取一次現價供保險絲判斷。相容舊版單張 sl_price
    sl_prices = info.get("sl_prices") or ([info["sl_price"]] if info.get("sl_price") else [])
    need_price = (SL_FAILSAFE and sl_prices) or (TP_FAILSAFE and info.get("tp_orders"))
    price = 0.0
    if need_price:
        if snap is not None:
            price = snap["prices"].get(symbol, 0.0)
        else:
            ticker = await _api(_client.futures_symbol_ticker, symbol=symbol)
            price = float(ticker.get("price") or 0)

    # 止損保險絲：價格穿過「最深的那道止損」但倉位還在 → 主動市價「全平」兜底
    if SL_FAILSAFE and sl_prices and price > 0:
        deepest = max(sl_prices) if TRADE_SIDE == "SHORT" else min(sl_prices)
        if _stop_breached(price, deepest):
            await _force_close_futures(trade, amt, price, deepest)
            return

    # 止盈保險絲：價格穿過某段止盈但該段條件單還掛著沒成交 → 市價「逐段補平」
    if TP_FAILSAFE and price > 0 and info.get("tp_orders"):
        if await _tp_failsafe_futures(trade, info, price, snap):
            return  # 有補平就這輪先收尾，下輪再判斷止損上移/收單

    # #2 雙軌動態止損：每段 TP 成交後，撤兩道止損、在階梯新階重掛（各取剩餘半倉）
    if BREAKEVEN_AFTER_TP1:
        await _update_dual_sl_futures(trade, info, amt, snap)


def _tp_breached(price: float, tp_price: float) -> bool:
    """價格是否已達止盈：做多 = 漲到；做空 = 跌到。"""
    if TRADE_SIDE == "SHORT":
        return price <= tp_price
    return price >= tp_price


async def _tp_failsafe_futures(trade: dict, info: dict, price: float,
                               snap: dict | None = None) -> bool:
    """逐段檢查：某段 TP 條件單還掛著、但價格已達該 TP → 市價補平那一段。回傳是否有動作。"""
    symbol = trade["symbol"]
    _, close_side = _sides()
    filt = await _get_filters(symbol)
    if snap is not None:
        open_ids = snap["open_cond_ids"]
    else:
        cond = await _api(_client.futures_get_open_orders, symbol=symbol, conditional=True)
        open_ids = {(o.get("orderId") or o.get("algoId")) for o in cond}

    acted = False
    for tp in info.get("tp_orders") or []:
        if tp.get("order_id") not in open_ids:
            continue  # 該段已成交或已取消
        if not _tp_breached(price, float(tp["tp"])):
            continue
        # 先撤掉該段 TP 條件單（避免等下又自己觸發重複賣），再市價補平該段數量
        await _cancel_conditional(symbol, tp["order_id"])
        qty = _quantize(float(tp["qty"]), filt["step"])
        try:
            await _api(_client.futures_create_order, symbol=symbol, side=close_side,
                       type="MARKET", quantity=format(qty, "f"), reduceOnly="true")
            print(f"[trader] ⚠️ trade#{trade['id']} {symbol} 現價 {price} 已達 "
                  f"TP{tp['level']} {tp['tp']} 但未成交 → 保險絲補平 {qty}")
            acted = True
        except BinanceAPIException as e:
            if e.code != -2022:  # -2022 reduceOnly 被拒：倉位已不足，忽略
                print(f"[trader] TP 保險絲補平失敗：{e.status_code} {e.message}")
    return acted


def _stop_breached(price: float, sl_price: float) -> bool:
    """價格是否已穿過止損：做多 = 跌破；做空 = 漲破。"""
    if TRADE_SIDE == "SHORT":
        return price >= sl_price
    return price <= sl_price


def _net_breakeven_price(entry: Decimal) -> Decimal:
    """淨保本價：把來回手續費算進去，移動後即使被掃也不虧。做多往上加、做空往下減。"""
    bump = Decimal(str(BREAKEVEN_FEE_PCT)) / Decimal("100")
    return entry * (Decimal(1) - bump) if TRADE_SIDE == "SHORT" else entry * (Decimal(1) + bump)


def _sl_safe_side(target: float, price: float) -> bool:
    """新止損是否在市價的安全側（做多：低於市價；做空：高於市價），避免一掛就立刻觸發。"""
    return target > price if TRADE_SIDE == "SHORT" else target < price


async def _force_close_futures(trade: dict, amt: float, price: float, sl_price: float) -> None:
    """保險絲：條件單沒觸發時，主動市價平掉整個倉位、撤殘留單、標記 CLOSED。"""
    symbol = trade["symbol"]
    _, close_side = _sides()
    filt = await _get_filters(symbol)
    qty = _quantize(amt, filt["step"])
    print(f"[trader] ⚠️ trade#{trade['id']} {symbol} 現價 {price} 已穿過止損 {sl_price}，"
          f"倉位未平 → 保險絲主動市價平倉 {qty}")
    try:
        await _api(_client.futures_create_order, symbol=symbol, side=close_side,
                   type="MARKET", quantity=format(qty, "f"), reduceOnly="true")
    except BinanceAPIException as e:
        # -2022 ReduceOnly 被拒：通常是條件單剛好同時觸發、倉位已平，下輪對帳會收單
        if e.code != -2022:
            print(f"[trader] 保險絲平倉失敗：{e.status_code} {e.message}")
        return
    await _cancel_all_futures_orders(symbol)
    trades.set_status(trade["id"], "CLOSED")
    print(f"[trader] trade#{trade['id']} {symbol} 保險絲已平倉 → CLOSED")


async def _cancel_conditional(symbol: str, order_id) -> None:
    """撤掉一張條件單（TP/SL）。這些是 closePosition/reduceOnly 的 algo 條件單，要用 algoId 撤
    （回傳結構沒有一般單的 type/orderId 欄位）。已不存在（-2011）忽略，其他錯誤記 log 但不中斷對帳。"""
    if not order_id:
        return
    try:
        await _api(_client.futures_cancel_algo_order, algoId=order_id)
    except BinanceAPIException as e:
        if e.code != -2011:
            print(f"[trader] 撤條件單 {order_id} 失敗（忽略續行）：{e.code} {e.message}")


def _signal_sl1_pct(signal: dict) -> float | None:
    """訊號 SL1 相對進場的距離（正的百分比）；缺、為 0 或方向不對則回 None。"""
    stops = signal.get("stops") or []
    if not stops:
        return None
    entry = float(signal["entry"])
    sl1 = float(stops[0]["price"])
    pct = (sl1 - entry) / entry * 100 if TRADE_SIDE == "SHORT" else (entry - sl1) / entry * 100
    return pct if pct > 0 else None


def _effective_sl_pcts(signal: dict) -> tuple[float, float]:
    """回傳 (SL1%, SL2%)：SL1 = min(訊號SL1, 上限 SL1_PCT)；SL2 = SL1 × SL2_MULT。
    訊號缺 SL1 或方向不對時，SL1 直接用上限。"""
    sig = _signal_sl1_pct(signal)
    sl1 = min(sig, SL1_PCT) if sig is not None else SL1_PCT
    return sl1, sl1 * SL2_MULT


def effective_sl1(signal: dict) -> dict:
    """通知用：實際下單會掛的單一 SL1（沿用 _effective_sl_pcts 的「訊號SL1 與上限取小」）。
    回 {level, price, pct}，價格與下單上軌一致；僅供顯示，未依交易對 tick 取整。"""
    entry = float(signal["entry"])
    sl1_pct, _ = _effective_sl_pcts(signal)
    price = entry * (1 + sl1_pct / 100) if TRADE_SIDE == "SHORT" else entry * (1 - sl1_pct / 100)
    return {"level": 1, "price": round(price, 8), "pct": round((price - entry) / entry * 100, 2)}


def _sl_ladder(signal: dict) -> list[float]:
    """止損階梯（防守→鎖利）：[SL2, SL1, 淨保本, TP1, TP2…]。
    SL1 = min(訊號SL1距離, 上限 SL1_PCT)；SL2 = SL1 × SL2_MULT。做多往下、做空往上。
    雙軌取相鄰兩階：下軌=rung[tier]、上軌=rung[tier+1]（tier=已成交TP數）。"""
    entry = Decimal(str(signal["entry"]))
    sl1_pct, sl2_pct = _effective_sl_pcts(signal)
    p1 = Decimal(str(sl1_pct)) / 100
    p2 = Decimal(str(sl2_pct)) / 100
    if TRADE_SIDE == "SHORT":
        sl2, sl1 = entry * (1 + p2), entry * (1 + p1)             # 做空：止損在上方
    else:
        sl2, sl1 = entry * (1 - p2), entry * (1 - p1)             # 做多：止損在下方
    rungs = [float(sl2), float(sl1), float(_net_breakeven_price(entry))]
    rungs.extend(float(t["price"]) for t in signal["targets"])    # TP1..TPn
    return rungs


async def _place_dual_sls(symbol: str, sl_hi: float, sl_lo: float,
                          remaining: float, filt: dict, dual: bool = True) -> tuple[list, list]:
    """掛 reduceOnly 止損。dual=True：雙軌半倉（上軌 sl_hi、下軌 sl_lo 各半）；
    dual=False：單軌守全倉（只掛 sl_hi）。回傳 (order_ids, prices)。
    某段已穿價(-2021)就市價平該段；雙軌倉位太小無法對半→退回單張守全部剩餘。"""
    _, close_side = _sides()
    min_qty = float(filt["min_qty"])
    hi = _quantize(sl_hi, filt["tick"])
    lo = _quantize(sl_lo, filt["tick"])
    if not dual:
        legs = [(hi, _quantize(remaining, filt["step"]))]       # 單一止損：守全倉
    else:
        half = _quantize(remaining / 2, filt["step"])
        rest = _quantize(remaining - float(half), filt["step"])
        if float(half) < min_qty or float(rest) < min_qty:
            legs = [(hi, _quantize(remaining, filt["step"]))]   # 太小→單張守全部剩餘，掛較保守的上軌
        else:
            legs = [(hi, half), (lo, rest)]

    portion = "半倉" if len(legs) > 1 else "全倉"
    ids, prices = [], []
    for price, qty in legs:
        if float(qty) <= 0:
            continue
        try:
            resp = await _api(_client.futures_create_order, symbol=symbol, side=close_side,
                              type="STOP_MARKET", stopPrice=format(price, "f"),
                              quantity=format(qty, "f"), reduceOnly="true")
            ids.append(_oid(resp))
            prices.append(float(price))
            print(f"[trader]   SL: {close_side} {qty} @ {price}（reduceOnly {portion}）")
        except BinanceAPIException as e:
            if e.code != -2021:
                raise
            print(f"[trader] ⚠️ {symbol} 止損 {price} 進場時已穿價 → 市價平 {qty}")
            await _market_close_qty(symbol, close_side, qty)
    return ids, prices


async def _tp_levels_realized(symbol: str, info: dict, snap: dict | None = None) -> int:
    """用「TP 掛單是否還在」判定已成交到第幾段（止盈按序成交）。
    以掛單為準，不用倉位減少量，避免止損先觸發造成倉位變少被誤判成 TP 成交。"""
    if snap is not None:
        open_ids = snap["open_cond_ids"]
    else:
        cond = await _api(_client.futures_get_open_orders, symbol=symbol, conditional=True)
        open_ids = {(o.get("orderId") or o.get("algoId")) for o in cond}
    tier = 0
    for t in sorted(info.get("tp_orders") or [], key=lambda x: x["level"]):
        if t["order_id"] not in open_ids:
            tier = t["level"]   # 此段 TP 已不在掛單 = 已實現
        else:
            break
    return tier


async def _update_dual_sl_futures(trade: dict, info: dict, amt: float,
                                  snap: dict | None = None) -> None:
    """雙軌動態止損：每多成交一段 TP，撤掉兩道舊止損，在階梯新一階重掛、各取當下剩餘的一半。
    上軌=rung[tier+1]、下軌=rung[tier]（rung=[訊號SL2, 訊號SL1×倍數, 淨保本, TP1, TP2…]）。"""
    if not info.get("tp_orders"):
        return
    symbol = trade["symbol"]
    async with _sl_update_lock:  # WS 即時事件與保險絲迴圈都可能進來，序列化避免重複撤/掛
        tier = await _tp_levels_realized(symbol, info, snap)
        # 鎖內重讀 sl_moved（以 DB 為準），避免兩個觸發源拿到過期值重複上移
        current = int((trades.get(trade["id"]) or {}).get("sl_moved") or 0)
        if tier < 1 or tier <= current:
            return  # 還沒多成交 TP，或止損已在這個（含更高）階

        filt = await _get_filters(symbol)
        rungs = _sl_ladder(trade["signal"])
        if tier + 1 >= len(rungs):
            return
        sl_hi = _quantize(rungs[tier + 1], filt["tick"])
        sl_lo = _quantize(rungs[tier], filt["tick"])

        if snap is not None:
            price = snap["prices"].get(symbol, 0.0)
        else:
            ticker = await _api(_client.futures_symbol_ticker, symbol=symbol)
            price = float(ticker["price"])
        if price <= 0 or not _sl_safe_side(float(sl_hi), price):
            return  # 上軌已不在市價安全側（設了會立刻觸發）→ 等下一次再試

        # 撤掉舊的兩道止損（含相容舊版單張 sl_order_id）
        for oid in (info.get("sl_orders") or []):
            await _cancel_conditional(symbol, oid)
        await _cancel_conditional(symbol, info.get("sl_order_id"))

        sl_ids, sl_prices = await _place_dual_sls(symbol, float(sl_hi), float(sl_lo), amt, filt,
                                                  dual=SL2_MULT > 0)
        info["sl_orders"] = sl_ids
        info["sl_prices"] = sl_prices
        info.pop("sl_order_id", None)
        info.pop("sl_price", None)
        trades.update_oco(trade["id"], info, sl_moved=tier)
        if len(sl_prices) > 1:
            detail = f"上軌 {sl_prices[0]} / 下軌 {sl_prices[1]}（各半倉）"
        else:
            detail = f"{sl_prices[0]}（全倉）"
        print(f"[trader] trade#{trade['id']} {symbol} 已實現 TP{tier} → 止損上移：{detail}")


async def _cancel_all_futures_orders(symbol: str) -> None:
    """撤掉某交易對的一般單與條件單（TP/SL），忽略「無單可撤」。"""
    for kwargs in ({"conditional": True}, {}):
        try:
            await _api(_client.futures_cancel_all_open_orders, symbol=symbol, **kwargs)
        except BinanceAPIException as e:
            if e.code != -2011:
                print(f"[trader] 撤單警告 {symbol} {kwargs}：{e.message}")


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

    be_price = _net_breakeven_price(entry)  # 淨保本：進場價 + 來回手續費
    ticker = await _api(_client.get_symbol_ticker, symbol=symbol)
    price = Decimal(ticker["price"])
    if be_price >= price:
        return  # 已回落到保本價附近，設保本會讓止損高於市價（OCO 不合法），維持原止損

    be_trigger = _quantize(float(be_price), filt["tick"])
    be_limit = _quantize(float(be_price * Decimal(str(1 - SL_LIMIT_BUFFER_PCT / 100))), filt["tick"])
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
