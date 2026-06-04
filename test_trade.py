"""在幣安 testnet 上一鍵驗證自動交易鏈路：下單 → 成交 → 掛 OCO。

不需要等真的 TG 訊號——本腳本會抓指定交易對的現價，組出一筆「假訊號」餵給
binance_trader.on_signal，跑完整流程。預設用 BTCUSDT（testnet 有支援）。

用法：
    python test_trade.py                  # 預設 BTCUSDT
    python test_trade.py --symbol ETHUSDT --usdt 60
    python test_trade.py --reset          # 先撤掉該幣未結單再測（可重複跑）

⚠️ 請確認 .env 的 BINANCE_TESTNET=1，否則會在正式網下真單！
"""

import argparse
import asyncio
import time

from dotenv import load_dotenv

load_dotenv()


def build_fake_signal(symbol: str, price: float) -> dict:
    """依現價組出合理的進場/目標/止損，讓限價買單能立刻成交。"""
    entry = price * 1.001  # 略高於現價 → 限價買單立即成交
    targets = [
        {"level": 1, "price": entry * 1.004, "pct": 0.4},
        {"level": 2, "price": entry * 1.008, "pct": 0.8},
        {"level": 3, "price": entry * 1.015, "pct": 1.5},
        {"level": 4, "price": entry * 1.030, "pct": 3.0},
    ]
    stops = [
        {"level": 1, "price": entry * 0.990, "pct": -1.0},
        {"level": 2, "price": entry * 0.970, "pct": -3.0},
    ]
    return {"symbol": symbol, "entry": entry, "targets": targets, "stops": stops}


async def reset_symbol(trader, symbol: str) -> None:
    """撤掉該交易對在交易所的未結單，並把本機 DB 中該幣的未結倉標記 CLOSED。"""
    import trades

    try:
        open_orders = await trader._api(trader._client.get_open_orders, symbol=symbol)
    except Exception as e:
        print(f"[reset] 取得未結單失敗：{e}")
        open_orders = []

    for o in open_orders:
        try:
            await trader._api(trader._client.cancel_order, symbol=symbol, orderId=o["orderId"])
            print(f"[reset] 已撤單 orderId={o['orderId']}")
        except Exception:
            pass  # OCO 撤一腿會連帶取消另一腿，第二次撤會失敗，忽略

    for status in ("PENDING_BUY", "ACTIVE"):
        for t in trades.list_status(status):
            if t["symbol"] == symbol:
                trades.set_status(t["id"], "CLOSED")
                print(f"[reset] trade#{t['id']} {symbol} → CLOSED")


async def wait_done(symbol: str, timeout: int = 180):
    import trades

    deadline = time.time() + timeout
    while time.time() < deadline:
        active = [t for t in trades.list_status("ACTIVE") if t["symbol"] == symbol]
        canceled = [t for t in trades.list_status("CANCELED") if t["symbol"] == symbol]
        if active:
            return "ACTIVE", active[-1]
        if canceled:
            return "CANCELED", canceled[-1]
        await asyncio.sleep(2)
    return "TIMEOUT", None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT", help="測試用交易對（testnet 要支援）")
    ap.add_argument("--usdt", type=float, default=None, help="覆寫每筆 USDT 金額")
    ap.add_argument("--reset", action="store_true", help="先撤掉該幣未結單再測")
    args = ap.parse_args()

    import binance_trader as trader

    if args.usdt is not None:
        trader.TRADE_USDT = args.usdt

    if not trader.TESTNET:
        confirm = input("⚠️ 目前 BINANCE_TESTNET=0（正式網真錢）！確定要繼續？輸入 yes：")
        if confirm.strip().lower() != "yes":
            print("已中止。")
            return

    trader.init()
    symbol = args.symbol.upper()

    if args.reset:
        await reset_symbol(trader, symbol)

    ticker = await trader._api(trader._client.get_symbol_ticker, symbol=symbol)
    price = float(ticker["price"])
    signal = build_fake_signal(symbol, price)
    print(f"\n=== 測試訊號 {symbol} ===")
    print(f"現價 {price} → 進場 {signal['entry']:.6f}")
    print(f"目標價：{[round(t['price'], 6) for t in signal['targets']]}")
    print(f"止損價：{[round(s['price'], 6) for s in signal['stops']]}（SL1 全清）\n")

    await trader.on_signal(signal)

    print("等待買單成交與 OCO 掛單…（最多 180 秒）")
    result, trade = await wait_done(symbol)
    if result == "ACTIVE":
        print(f"\n✅ 鏈路驗證成功！trade#{trade['id']} {symbol} 已成交並掛好 OCO。")
        print("到幣安 testnet 的『現貨訂單 → 當前委託』可看到 4 張 OCO 止盈止損單。")
    elif result == "CANCELED":
        print(f"\n⚠️ 買單未成交被取消（trade#{trade['id']}）。可加大 --usdt 或檢查交易對。")
    else:
        print("\n⏱️ 逾時：買單可能尚未成交。檢查上方是否有 [trader] 略過訊息（金額太小/交易對不支援）。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已中止。")
