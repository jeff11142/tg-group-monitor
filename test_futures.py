"""離線測試：合約（Futures）下單與對帳邏輯。不連網。

注入假 futures client，驗證：
- 進場 → 成交 → 掛 4 張 reduce-only 止盈 + 2 張 reduce-only 雙軌半倉止損
- 倉位歸零 → 自動收單 CLOSED
- 倉位減少（止盈成交）→ 雙軌止損上移一階

跑法：python test_futures.py
"""

import asyncio
import tempfile
from decimal import Decimal

import trades
import binance_trader as bt
from binance.exceptions import BinanceAPIException


def _api_error(code: int):
    class _R:
        status_code = 400
        text = '{"code": %d, "msg": "Order would immediately trigger."}' % code
    return BinanceAPIException(_R(), 400, _R.text)


class FakeFuturesClient:
    def __init__(self, price="100"):
        self.price = price
        self.created = []
        self.canceled = []
        self.cancel_all_count = 0
        self.leverage = None
        self.margin = None
        self.position_amt = "0"
        self.order_status = "FILLED"
        self.executed_qty = "0"
        self.open_conditional = []  # 還掛著的條件單（TP/SL）
        self.stop_error = None      # 設成 error code → 掛 STOP_MARKET 時丟該錯
        self._oid = 7000

    def futures_exchange_info(self):
        return {"symbols": [{
            "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]}]}

    def futures_change_leverage(self, symbol, leverage):
        self.leverage = leverage
        return {}

    def futures_change_margin_type(self, symbol, marginType):
        self.margin = marginType
        return {}

    def futures_create_order(self, **kw):
        if self.stop_error and kw.get("type") == "STOP_MARKET":
            raise _api_error(self.stop_error)
        self._oid += 1
        self.created.append({**kw, "orderId": self._oid})
        if kw.get("type") == "LIMIT":
            self.position_amt = kw.get("quantity", "0")  # 模擬進場成交開倉
            return {"orderId": self._oid, "status": "FILLED",
                    "executedQty": kw.get("quantity", "0")}
        # 條件單（TAKE_PROFIT_MARKET / STOP_MARKET）回 algoId、不含 orderId（如真實合約）
        if kw.get("type") in ("TAKE_PROFIT_MARKET", "STOP_MARKET"):
            self.open_conditional.append({"algoId": self._oid, **kw})
        return {"algoId": self._oid}

    def futures_get_order(self, symbol, orderId):
        return {"status": self.order_status, "executedQty": self.executed_qty, "orderId": orderId}

    def futures_position_information(self, symbol):
        return [{"symbol": symbol, "positionAmt": self.position_amt}]

    def futures_get_open_orders(self, symbol, conditional=False):
        return list(self.open_conditional) if conditional else []

    def futures_cancel_algo_order(self, algoId=None, **kw):
        self.canceled.append(algoId)
        self.open_conditional = [o for o in self.open_conditional if o["algoId"] != algoId]
        return {}

    def futures_cancel_all_open_orders(self, symbol, **kw):
        self.cancel_all_count += 1
        self.open_conditional = []
        return {}

    def futures_symbol_ticker(self, symbol):
        return {"price": self.price}


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    assert cond, f"測試失敗：{name}"


SIGNAL = {
    "symbol": "BTCUSDT", "entry": 100.0,
    "targets": [{"level": i + 1, "price": 100 * (1 + 0.01 * (i + 1)), "pct": 0} for i in range(4)],
    "stops": [{"level": 1, "price": 95.0, "pct": 0}],
}


async def scenario_entry_and_protection():
    print("\n[合約-1] 進場成交 → 掛 4 止盈(reduceOnly) + 2 止損(reduceOnly 雙軌半倉)")
    fake = bt._client = FakeFuturesClient()
    bt._filters_cache.clear()
    await bt.on_signal(SIGNAL)
    await asyncio.sleep(0.2)
    check("有設定槓桿", fake.leverage == bt.LEVERAGE)
    check("有設定保證金模式", fake.margin == bt.MARGIN_TYPE)
    entries = [c for c in fake.created if c.get("type") == "LIMIT"]
    tps = [c for c in fake.created if c.get("type") == "TAKE_PROFIT_MARKET"]
    sls = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
    check("1 張進場 LIMIT 單", len(entries) == 1)
    check("4 張 reduce-only 止盈", len(tps) == 4 and all(t["reduceOnly"] == "true" for t in tps))
    check("2 張 reduce-only 雙軌止損", len(sls) == 2 and all(s["reduceOnly"] == "true" for s in sls))
    check("止損價在 SL1=95、SL2=90", {s["stopPrice"] for s in sls} == {"95.00", "90.00"})
    t = trades.list_status("ACTIVE")[-1]
    check("交易進入 ACTIVE 且記下合約保護單", t["oco_orders"].get("mode") == "futures")
    return t["id"]


async def scenario_position_closed():
    print("\n[合約-2] 倉位歸零 → 自動收單 CLOSED")
    fake = bt._client = FakeFuturesClient()
    bt._filters_cache.clear()
    await bt.on_signal(SIGNAL)
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    fake.position_amt = "0"  # 倉位已平
    await bt._reconcile(trades.get(tid))
    check("撤掉殘留單（含條件單）", fake.cancel_all_count >= 1)
    check("交易標記 CLOSED", trades.get(tid)["status"] == "CLOSED")


async def scenario_breakeven():
    print("\n[合約-3] TP1 成交 → 雙軌止損上移一階（下軌→SL1、上軌→淨保本）")
    fake = bt._client = FakeFuturesClient(price="103")  # 現價高於進場
    bt._filters_cache.clear()
    bt.TP_FAILSAFE = False  # 隔離測試止損上移，不讓 TP 保險絲搶先觸發
    await bt.on_signal(SIGNAL)
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    info = trades.get(tid)["oco_orders"]
    # 模擬 TP1 成交：新版以「TP 條件單是否還掛著」判定已實現第幾段，故把 TP1 從掛單移除
    tp1_id = next(o["order_id"] for o in info["tp_orders"] if o["level"] == 1)
    fake.open_conditional = [o for o in fake.open_conditional if o["algoId"] != tp1_id]
    fake.position_amt = str(trades.get(tid)["qty"] - 0.09)  # 倉位減去 TP1 那段
    fake.canceled.clear()
    before_sl = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
    await bt._reconcile(trades.get(tid))
    after_sl = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
    new_sl = after_sl[len(before_sl):]
    t = trades.get(tid)
    be = format(bt._quantize(float(bt._net_breakeven_price(Decimal(str(SIGNAL["entry"])))), "0.01"), "f")
    check("撤掉兩道舊止損", len(fake.canceled) == 2)
    check("重掛兩道新止損（各半倉 reduceOnly）",
          len(new_sl) == 2 and all(s["reduceOnly"] == "true" for s in new_sl))
    check(f"上軌移到淨保本 {be}、下軌移到 SL1=95",
          {s["stopPrice"] for s in new_sl} == {be, "95.00"})
    check("sl_moved = 1（已上移一階）", t["sl_moved"] == 1)


async def scenario_timeout_but_filled():
    print("\n[合約-4] 超時瞬間其實已成交 → 不誤判取消，照掛保護單")
    fake = bt._client = FakeFuturesClient()
    fake.order_status = "FILLED"  # 超時時重查顯示已成交
    bt._filters_cache.clear()
    tid = trades.add("BTCUSDT", 100, 0.3, 888, SIGNAL)
    trades.set_status(tid, "PENDING_BUY")
    # 傳入「上一次輪詢的舊狀態」NEW，模擬剛好在超時瞬間成交
    result = await bt._handle_entry_timeout(tid, "BTCUSDT", 888,
                                            {"status": "NEW", "executedQty": "0"})
    check("回傳需掛保護單(True)", result is True)
    check("交易未被誤標 CANCELED", trades.get(tid)["status"] != "CANCELED")
    check("沒有去撤單（因已成交）", len(fake.canceled) == 0)


async def scenario_sl_failsafe():
    print("\n[合約-5] 價格穿過止損但倉位還在 → 保險絲主動市價平倉")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="89")  # 現價 89 < 最深止損 SL2=90（雙軌保險絲看最深那道）
    bt._filters_cache.clear()
    bt.SL_FAILSAFE = True
    bt.TP_FAILSAFE = True
    await bt.on_signal(SIGNAL)  # SL=95
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    fake.position_amt = "0.3"   # 倉位還在（條件單沒觸發）
    fake.created.clear()
    await bt._reconcile(trades.get(tid))
    closes = [c for c in fake.created if c.get("type") == "MARKET" and c.get("reduceOnly") == "true"]
    check("有送出市價平倉單", len(closes) == 1)
    check("方向是 SELL（平多）", closes[0]["side"] == "SELL")
    check("撤掉殘留條件單", fake.cancel_all_count >= 1)
    check("交易標記 CLOSED", trades.get(tid)["status"] == "CLOSED")


async def scenario_sl_failsafe_not_breached():
    print("\n[合約-6] 價格在止損上方、也未達任何止盈 → 保險絲不動作")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="100.5")  # 高於 SL 95、低於 TP1 101
    bt._filters_cache.clear()
    bt.SL_FAILSAFE = True
    bt.TP_FAILSAFE = True
    await bt.on_signal(SIGNAL)
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    fake.position_amt = "0.3"
    fake.created.clear()
    await bt._reconcile(trades.get(tid))
    check("沒有市價平倉", not any(c.get("type") == "MARKET" for c in fake.created))
    check("交易維持 ACTIVE", trades.get(tid)["status"] == "ACTIVE")


async def scenario_tp_failsafe():
    print("\n[合約-7] 價格達 TP1/TP2 但沒成交 → 止盈逐段市價補平")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="102.5")  # 過 TP1(101)/TP2(102)，未到 TP3(103)
    bt._filters_cache.clear()
    bt.TP_FAILSAFE = True
    bt.SL_FAILSAFE = True
    await bt.on_signal(SIGNAL)  # TP 101/102/103/104，SL 95
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    fake.position_amt = "0.3"
    fake.created.clear()
    await bt._reconcile(trades.get(tid))
    closes = [c for c in fake.created if c.get("type") == "MARKET"]
    check("只補平 2 段（TP1, TP2）", len(closes) == 2)
    check("都是 SELL reduceOnly 部分平倉", all(
        c["side"] == "SELL" and c["reduceOnly"] == "true" for c in closes))
    check("撤掉那 2 張 TP 條件單", len(fake.canceled) == 2)
    check("沒有全平（倉位未歸零、未 CLOSED）", trades.get(tid)["status"] == "ACTIVE")


async def scenario_sl_immediate_trigger():
    print("\n[合約-8] 進場時價格已穿過止損（掛 SL 被 -2021 拒）→ 直接市價全平、不留裸倉")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="94")
    fake.stop_error = -2021      # 掛止損被拒（價格已破止損）
    fake.position_amt = "0.3"    # 進場已成交
    bt._filters_cache.clear()
    await bt.on_signal(SIGNAL)   # SL=95，現價 94 已破
    await asyncio.sleep(0.2)
    check("沒留下 ACTIVE 裸倉", len(trades.list_status("ACTIVE")) == 0)
    closed = [t for t in trades.list_status("CLOSED") if t["symbol"] == "BTCUSDT"]
    check("交易被直接平倉 → CLOSED", len(closed) >= 1)
    closes = [c for c in fake.created if c.get("type") == "MARKET"]
    check("有送出市價平倉單", len(closes) >= 1)


async def scenario_no_position_on_protect():
    print("\n[合約-9] 掛保護時已無持倉（重啟回復/已平倉）→ 不掛、直接 CLOSED")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient()
    fake.position_amt = "0"  # 無持倉
    bt._filters_cache.clear()
    tid = trades.add("BTCUSDT", 100, 0.3, 999, SIGNAL)
    trades.set_status(tid, "PENDING_BUY")
    ok = await bt._place_protection_futures(trades.get(tid))
    check("回傳 False（沒掛保護）", ok is False)
    check("沒送出任何條件單", not any(
        c.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET") for c in fake.created))
    check("交易標記 CLOSED", trades.get(tid)["status"] == "CLOSED")


async def scenario_sl_place_fail_failsafe():
    print("\n[合約-10] 止損單掛不上(非-2021) → 不中斷、存sl_price交保險絲、仍 ACTIVE")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="100")  # 未破 SL(95)
    fake.stop_error = -4136  # STOP_MARKET 掛單失敗（模擬 closePosition 失靈）
    bt._filters_cache.clear()
    await bt.on_signal(SIGNAL)  # SL=95
    await asyncio.sleep(0.2)
    active = trades.list_status("ACTIVE")
    check("交易仍進 ACTIVE（沒卡 PENDING_BUY）", len(active) == 1)
    info = active[0]["oco_orders"]
    check("有存 sl_prices 供保險絲（含 SL1=95）", 95.0 in (info.get("sl_prices") or []))
    check("交易所 SL 沒掛上（sl_orders 為空）", not info.get("sl_orders"))
    check("4 段 TP 仍正常掛上", len(info.get("tp_orders")) == 4)


async def scenario_raw_signal_mode():
    print("\n[合約-11] 原始訊號做單：止損照訊號原始 SL1/SL2，不走重算")
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    fake = bt._client = FakeFuturesClient(price="100")
    bt._filters_cache.clear()
    bt.RAW_SIGNAL_MODE = True
    # stops 是主流程覆蓋後的單一 SL1；source_stops 是訊號原始 SL1/SL2。raw 模式應採用 source_stops。
    raw_signal = {
        "symbol": "BTCUSDT", "entry": 100.0,
        "targets": [{"level": i + 1, "price": 100 * (1 + 0.01 * (i + 1)), "pct": 0} for i in range(4)],
        "stops": [{"level": 1, "price": 97.0, "pct": 0}],
        "source_stops": [{"level": 1, "price": 96.0, "pct": 0}, {"level": 2, "price": 93.0, "pct": 0}],
    }
    try:
        await bt.on_signal(raw_signal)
        await asyncio.sleep(0.2)
        sls = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
        check("2 張雙軌止損", len(sls) == 2)
        check("止損照訊號原始 SL1=96、SL2=93（非重算的 95/90）",
              {s["stopPrice"] for s in sls} == {"96.00", "93.00"})
    finally:
        bt.RAW_SIGNAL_MODE = False


async def main():
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    bt.FUTURES = True
    bt.TRADE_SIDE = "LONG"
    bt.LEVERAGE = 1
    bt.MARGIN_TYPE = "ISOLATED"
    bt.TRADE_USDT = 30.0
    bt.TP_RATIOS = [30, 30, 20, 20]
    bt.MAX_OPEN_TRADES = 50
    await scenario_entry_and_protection()
    await scenario_position_closed()
    await scenario_breakeven()
    await scenario_timeout_but_filled()
    await scenario_sl_failsafe()
    await scenario_sl_failsafe_not_breached()
    await scenario_tp_failsafe()
    await scenario_sl_immediate_trigger()
    await scenario_no_position_on_protect()
    await scenario_sl_place_fail_failsafe()
    await scenario_raw_signal_mode()
    print("\n🎉 合約交易邏輯測試全部通過")


if __name__ == "__main__":
    asyncio.run(main())
