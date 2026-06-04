"""離線測試對帳迴圈邏輯（#1 自動收單、#2 TP1 後移動停損到保本）。

不連幣安：注入一個假 client，控制「目前還掛著哪些 OCO、現價多少」，
直接驗證 _reconcile / _move_sl_to_breakeven 的行為是否正確。

跑法：python test_reconcile.py
"""

import asyncio
import tempfile
from decimal import Decimal

import trades
import binance_trader as bt


class FakeClient:
    """模擬幣安回應：記錄撤單/掛單，並維護「目前掛著哪些 OCO」。"""

    def __init__(self, price="65000"):
        self.open_orders = []      # 每張 OCO 兩腿，各含 orderId / orderListId
        self.price = price
        self.canceled = []         # 被撤掉的 orderId
        self.created = []          # 新掛的 OCO 參數
        self._next_list_id = 1000

    def get_symbol_info(self, symbol):
        return {"baseAsset": "BTC", "quoteAsset": "USDT", "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ]}

    def get_open_orders(self, symbol):
        return list(self.open_orders)

    def get_symbol_ticker(self, symbol):
        return {"price": self.price}

    def cancel_order(self, symbol, orderId):
        self.canceled.append(orderId)
        o = next((x for x in self.open_orders if x["orderId"] == orderId), None)
        if o:  # 撤一腿 = 整張 OCO 取消
            lid = o["orderListId"]
            self.open_orders = [x for x in self.open_orders if x["orderListId"] != lid]
        return {}

    def create_oco_order(self, **kw):
        self._next_list_id += 1
        lid = self._next_list_id
        self.created.append({**kw, "orderListId": lid})
        self.open_orders.append({"orderId": lid * 10 + 1, "orderListId": lid})
        self.open_orders.append({"orderId": lid * 10 + 2, "orderListId": lid})
        return {"orderListId": lid}

    # 工具：把某張 OCO（依 list_id）標記為已成交 = 從掛單移除
    def fill_list(self, lid):
        self.open_orders = [x for x in self.open_orders if x["orderListId"] != lid]


ENTRY = 63936.07
LEG_DEFS = [(1, 0.00023, 64191.81), (2, 0.00023, 64447.56),
            (3, 0.00015, 64895.11), (4, 0.00016, 65854.15)]


def seed_active_trade(fake):
    """建立一筆 ACTIVE 交易 + 4 張掛著的 OCO，回傳 trade id。"""
    bt._filters_cache.clear()
    tid = trades.add("BTCUSDT", ENTRY, 0.00077, 999, {"symbol": "BTCUSDT"})
    trades.set_status(tid, "ACTIVE")
    legs = []
    fake.open_orders = []
    for lid, qty, tp in LEG_DEFS:
        fake.open_orders.append({"orderId": lid * 10 + 1, "orderListId": lid})
        fake.open_orders.append({"orderId": lid * 10 + 2, "orderListId": lid})
        legs.append({"list_id": lid, "qty": qty, "tp": tp, "level": lid})
    trades.set_oco(tid, legs)
    return tid


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    assert cond, f"測試失敗：{name}"


async def scenario_full_close():
    print("\n[情境1] 全部 OCO 平倉 → 自動收單 CLOSED")
    fake = bt._client = FakeClient()
    tid = seed_active_trade(fake)
    fake.open_orders = []  # 模擬全部成交
    await bt._reconcile(trades.get(tid))
    t = trades.get(tid)
    check("交易被標記 CLOSED", t["status"] == "CLOSED")
    check("不再計入 open 額度", trades.count_open() == 0)


async def scenario_tp1_breakeven():
    print("\n[情境2] TP1 成交（剩 3 張）→ 移動止損到保本")
    fake = bt._client = FakeClient(price="65000")  # 現價高於進場價
    tid = seed_active_trade(fake)
    fake.fill_list(1)  # 模擬 TP1（list_id=1）成交
    await bt._reconcile(trades.get(tid))
    t = trades.get(tid)
    check("撤掉剩餘 3 張舊 OCO", len(fake.canceled) == 3)
    check("重掛 3 張新 OCO", len(fake.created) == 3)
    be = format(bt._quantize(ENTRY, "0.01"), "f")
    check(f"新 OCO 止損都在保本價 {be}",
          all(c["belowStopPrice"] == be for c in fake.created))
    check("止盈價維持不變（4 個原始 TP 中的 3 個）",
          {c["abovePrice"] for c in fake.created}
          == {format(bt._quantize(tp, "0.01"), "f") for _, _, tp in LEG_DEFS[1:]})
    check("sl_moved 旗標已設", t["sl_moved"] == 1)
    check("leg 的 list_id 已更新成新單", all(lg["list_id"] > 1000 for lg in t["oco_orders"] if lg["level"] != 1))
    return tid


async def scenario_breakeven_then_close():
    print("\n[情境3] 移保本後剩餘全平 → 收單 CLOSED")
    fake = bt._client = FakeClient(price="65000")
    tid = seed_active_trade(fake)
    fake.fill_list(1)
    await bt._reconcile(trades.get(tid))   # 先移保本
    fake.open_orders = []                   # 模擬剩餘全部平倉
    await bt._reconcile(trades.get(tid))
    check("交易最終 CLOSED", trades.get(tid)["status"] == "CLOSED")


async def scenario_pullback_no_move():
    print("\n[情境4] 價格回落到進場價以下 → 不移保本（避免非法止損）")
    fake = bt._client = FakeClient(price="63000")  # 現價低於進場價
    tid = seed_active_trade(fake)
    fake.fill_list(1)
    await bt._reconcile(trades.get(tid))
    t = trades.get(tid)
    check("沒有撤單", len(fake.canceled) == 0)
    check("沒有重掛", len(fake.created) == 0)
    check("sl_moved 維持 0", t["sl_moved"] == 0)


async def main():
    trades.DB_FILE = tempfile.mktemp(suffix=".db")
    trades.init()
    await scenario_full_close()
    await scenario_tp1_breakeven()
    await scenario_breakeven_then_close()
    await scenario_pullback_no_move()
    print("\n🎉 全部對帳邏輯測試通過")


if __name__ == "__main__":
    asyncio.run(main())
