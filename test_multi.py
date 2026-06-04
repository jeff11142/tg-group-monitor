"""離線測試：15 個幣種訊號同時湧入，驗證持倉上限與多幣並存。

用假 client 同時觸發 15 個不同幣種的 on_signal，確認：
- 守住 MAX_OPEN_TRADES 上限（並發下也不超開）
- 開出的每個幣各自獨立、各有自己的 4 張 OCO

跑法：python test_multi.py
"""

import asyncio
import tempfile

import trades
import binance_trader as bt


class FakeClient:
    def __init__(self):
        self.created = []
        self.open_orders = []
        self._lid = 1000
        self._oid = 5000

    def get_symbol_info(self, symbol):
        return {"baseAsset": symbol.replace("USDT", ""), "quoteAsset": "USDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ]}

    def order_limit_buy(self, symbol, quantity, price):
        self._oid += 1
        return {"orderId": self._oid, "status": "FILLED"}  # 假設立即成交

    def get_order(self, symbol, orderId):
        return {"status": "FILLED", "executedQty": "0", "orderId": orderId}

    def create_oco_order(self, **kw):
        self._lid += 1
        lid = self._lid
        self.created.append({**kw, "orderListId": lid})
        self.open_orders.append({"orderId": lid * 10 + 1, "orderListId": lid})
        self.open_orders.append({"orderId": lid * 10 + 2, "orderListId": lid})
        return {"orderListId": lid}

    def get_open_orders(self, symbol):
        return []

    def cancel_order(self, **kw):
        return {}


def make_signal(i):
    e = 100.0
    return {
        "symbol": f"C{i}USDT",
        "entry": e,
        "targets": [{"level": j + 1, "price": e * (1 + 0.01 * (j + 1)), "pct": 0} for j in range(4)],
        "stops": [{"level": 1, "price": e * 0.95, "pct": 0}],
    }


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    assert cond, f"測試失敗：{name}"


async def main():
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    bt._client = FakeClient()
    bt._filters_cache.clear()
    bt.MAX_OPEN_TRADES = 5

    N = 15
    print(f"\n同時送出 {N} 個不同幣種訊號（MAX_OPEN_TRADES={bt.MAX_OPEN_TRADES}）")
    await asyncio.gather(*[bt.on_signal(make_signal(i)) for i in range(N)])
    await asyncio.sleep(0.3)  # 等背景 watcher 把 OCO 掛完

    opened = trades.list_status("ACTIVE") + trades.list_status("PENDING_BUY")
    syms = sorted({t["symbol"] for t in opened})
    print(f"實際開出 {len(opened)} 筆：{syms}")

    check("並發下仍守住上限（剛好 5 筆）", len(opened) == 5)
    check("5 筆都是不同幣種", len(syms) == 5)
    check("其餘 10 個訊號被擋下", len(opened) == 5)

    active = trades.list_status("ACTIVE")
    check("開出的都進入 ACTIVE", len(active) == 5)
    check("每筆都各有 4 張 OCO", all(len(t["oco_orders"]) == 4 for t in active))
    check("OCO 總數 = 5 幣 × 4 = 20", len(bt._client.created) == 20)

    print("\n🎉 多幣種並發測試通過：上限守住、各幣 OCO 獨立並存")


if __name__ == "__main__":
    asyncio.run(main())
