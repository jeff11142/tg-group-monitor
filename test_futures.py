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


class FakeFuturesClient:
    def __init__(self, price="100"):
        self.price = price
        self.created = []
        self.canceled = []
        self.cancel_all_count = 0
        self.leverage = None
        self.margin = None
        self.position_amt = "0"
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
        self._oid += 1
        self.created.append({**kw, "orderId": self._oid})
        if kw.get("type") == "LIMIT":
            return {"orderId": self._oid, "status": "FILLED",
                    "executedQty": kw.get("quantity", "0")}
        return {"orderId": self._oid}

    def futures_get_order(self, symbol, orderId):
        return {"status": "FILLED", "executedQty": "0", "orderId": orderId}

    def futures_position_information(self, symbol):
        return [{"symbol": symbol, "positionAmt": self.position_amt}]

    def futures_cancel_order(self, symbol, orderId):
        self.canceled.append(orderId)
        return {}

    def futures_cancel_all_open_orders(self, symbol):
        self.cancel_all_count += 1
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
    check("撤掉所有殘留單", fake.cancel_all_count == 1)
    check("交易標記 CLOSED", trades.get(tid)["status"] == "CLOSED")


async def scenario_breakeven():
    print("\n[合約-3] 倉位減少（止盈成交）→ 止損移到保本")
    fake = bt._client = FakeFuturesClient(price="103")  # 現價高於進場
    bt._filters_cache.clear()
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
    print("\n🎉 合約交易邏輯測試全部通過")


if __name__ == "__main__":
    asyncio.run(main())
