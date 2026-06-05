"""離線測試：合約（Futures）下單與對帳邏輯。不連網。

注入假 futures client，驗證：
- 進場 → 成交 → 掛 4 張 reduce-only 止盈 + 1 張 closePosition 止損
- 倉位歸零 → 自動收單 CLOSED
- 倉位減少（止盈成交）→ 止損移到保本

跑法：python test_futures.py
"""

import asyncio
import tempfile

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
        self.reject_stop = False    # True=掛 STOP_MARKET 時丟 -2021（模擬已穿過止損）
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
        if self.reject_stop and kw.get("type") == "STOP_MARKET":
            raise _api_error(-2021)
        self._oid += 1
        self.created.append({**kw, "orderId": self._oid})
        if kw.get("type") == "LIMIT":
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
    print("\n[合約-1] 進場成交 → 掛 4 止盈(reduceOnly) + 1 止損(closePosition)")
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
    check("1 張 closePosition 止損", len(sls) == 1 and sls[0]["closePosition"] == "true")
    check("止損價在 SL1=95", sls[0]["stopPrice"] == "95.00")
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
    print("\n[合約-3] 倉位減少（止盈成交）→ 止損移到保本")
    fake = bt._client = FakeFuturesClient(price="103")  # 現價高於進場
    bt._filters_cache.clear()
    bt.TP_FAILSAFE = False  # 隔離測試移保本，不讓 TP 保險絲搶先觸發
    await bt.on_signal(SIGNAL)
    await asyncio.sleep(0.2)
    tid = trades.list_status("ACTIVE")[-1]["id"]
    orig_qty = trades.get(tid)["qty"]
    fake.position_amt = str(orig_qty * 0.6)  # 倉位剩 60% = 有止盈成交
    fake.canceled.clear()
    before_sl = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
    await bt._reconcile(trades.get(tid))
    after_sl = [c for c in fake.created if c.get("type") == "STOP_MARKET"]
    t = trades.get(tid)
    check("撤掉舊止損", len(fake.canceled) == 1)
    check("重掛新止損在保本價 100", after_sl[-1]["stopPrice"] == "100.00")
    check("新止損仍是 closePosition", after_sl[-1]["closePosition"] == "true")
    check("sl_moved 已設", t["sl_moved"] == 1)
    check("多掛了一張止損", len(after_sl) == len(before_sl) + 1)


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
    fake = bt._client = FakeFuturesClient(price="94")  # 現價 94 < SL 95
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
    fake.reject_stop = True      # 掛止損會被拒（價格已破止損）
    fake.position_amt = "0.3"    # 進場已成交
    bt._filters_cache.clear()
    await bt.on_signal(SIGNAL)   # SL=95，現價 94 已破
    await asyncio.sleep(0.2)
    check("沒留下 ACTIVE 裸倉", len(trades.list_status("ACTIVE")) == 0)
    closed = [t for t in trades.list_status("CLOSED") if t["symbol"] == "BTCUSDT"]
    check("交易被直接平倉 → CLOSED", len(closed) >= 1)
    closes = [c for c in fake.created if c.get("type") == "MARKET"]
    check("有送出市價平倉單", len(closes) >= 1)


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
    print("\n🎉 合約交易邏輯測試全部通過")


if __name__ == "__main__":
    asyncio.run(main())
