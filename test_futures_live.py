"""連線測試：在幣安「合約測試網」實際丟一筆假訊號，跑完整下單流程。

⚠️ 這支會真的呼叫幣安 API 下單。預設只允許在 testnet（假錢）執行；
   想對正式網（真錢）跑必須明確加 --i-know-real，否則直接拒絕。

它做的事：
  1. 讀 .env、init 交易模組
  2. 抓該交易對現價，據此自動算「進場價 / 目標價 / 停損價」
  3. 呼叫 trader.on_signal() 跑完整流程（限價進場 → 成交 → 掛止盈止損）
  4. 等一段時間讓背景任務跑完，最後印出交易紀錄與交易所掛單

用法：
    python test_futures_live.py                       # BTCUSDT，市價附近進場、立即成交
    python test_futures_live.py --symbol ETHUSDT --usdt 100
    python test_futures_live.py --entry-offset-pct -2 # 進場價設在現價下方 2%（會掛著等成交）
    python test_futures_live.py --wait 120            # 等 120 秒觀察對帳
"""

import argparse
import asyncio

from dotenv import load_dotenv

load_dotenv()

import binance_trader as bt  # noqa: E402  (load_dotenv 要先跑)
import trades  # noqa: E402


def _fmt_orders(orders: list) -> None:
    """一般單與條件單(TP/SL)欄位不同，兩種都相容顯示。"""
    if not orders:
        print("  （無）")
        return
    for o in orders:
        oid = o.get("orderId") or o.get("algoId")
        otype = o.get("type") or o.get("orderType")
        qty = o.get("origQty") or o.get("quantity")
        trig = o.get("stopPrice") or o.get("triggerPrice") or "0"
        trig_txt = f" trig={trig}" if trig and float(trig) > 0 else ""
        extra = []
        if o.get("reduceOnly"):
            extra.append("reduceOnly")
        if o.get("closePosition"):
            extra.append("closePosition")
        tag = f" [{','.join(extra)}]" if extra else ""
        print(f"  #{oid} {o['side']} {otype} qty={qty}{trig_txt}{tag}")


async def run(args: argparse.Namespace) -> None:
    # ---- 安全檢查：預設只准 testnet ----
    if not bt.TESTNET and not args.i_know_real:
        raise SystemExit(
            "❌ 目前 BINANCE_TESTNET=0（正式網／真錢）。\n"
            "   這支是測試腳本，預設拒絕對真錢下單。\n"
            "   若你真的要對正式網跑，請加 --i-know-real（後果自負）。"
        )
    if not bt.FUTURES:
        raise SystemExit("❌ BINANCE_FUTURES 不是 1，這支只測合約。請在 .env 設 BINANCE_FUTURES=1。")

    bt.init()

    symbol = args.symbol.upper()
    client = bt._client

    # ---- 抓現價，據此算進場 / 目標 / 停損 ----
    ticker = await asyncio.to_thread(client.futures_symbol_ticker, symbol=symbol)
    market = float(ticker["price"])
    entry = args.entry if args.entry else market * (1 + args.entry_offset_pct / 100)

    # 做多：目標價在進場上方、停損在下方（百分比可調）
    targets = [
        {"level": i + 1, "price": entry * (1 + p / 100), "pct": p}
        for i, p in enumerate(args.tp_pcts)
    ]
    stops = [{"level": 1, "price": entry * (1 - args.sl_pct / 100), "pct": -args.sl_pct}]

    signal = {"symbol": symbol, "entry": entry, "targets": targets, "stops": stops}

    print("\n=== 即將送出的測試訊號 ===")
    print(f"  交易對   : {symbol}（現價 {market}）")
    print(f"  進場價   : {entry:.6g}"
          + ("（市價上方，預期立即成交）" if entry >= market else "（市價下方，會掛著等成交）"))
    print(f"  目標價   : {[round(t['price'], 6) for t in targets]}")
    print(f"  停損價   : {round(stops[0]['price'], 6)}")
    print(f"  每筆金額 : {bt.TRADE_USDT} USDT | 槓桿 {bt.LEVERAGE}x {bt.MARGIN_TYPE} | "
          f"分批 {bt.TP_RATIOS}")
    net = "TESTNET 測試網（假錢）" if bt.TESTNET else "⚠️ 正式網（真錢）"
    print(f"  環境     : {net}")

    if not args.yes:
        ans = input("\n確定送出？(y/N) ").strip().lower()
        if ans != "y":
            print("已取消。")
            return

    # ---- 送訊號，跑完整流程 ----
    await bt.on_signal(signal)

    print(f"\n等待 {args.wait} 秒讓背景任務（成交監控／對帳）執行…")
    # 期間順手跑幾輪對帳，模擬 _monitor_loop
    elapsed = 0
    while elapsed < args.wait:
        await asyncio.sleep(min(args.tick, args.wait - elapsed))
        elapsed += args.tick
        for t in trades.list_status("ACTIVE"):
            if t["symbol"] == symbol:
                await bt._reconcile(t)

    # ---- 結果 ----
    print("\n=== 本地交易紀錄 ===")
    for status in ("PENDING_BUY", "ACTIVE", "CLOSED", "CANCELED"):
        for t in trades.list_status(status):
            if t["symbol"] == symbol:
                print(f"  trade#{t['id']} {t['symbol']} status={t['status']} "
                      f"entry={t['entry']} qty={t['qty']} sl_moved={t.get('sl_moved')}")

    print(f"\n=== {symbol} 交易所一般掛單 ===")
    orders = await asyncio.to_thread(client.futures_get_open_orders, symbol=symbol)
    _fmt_orders(orders)
    print(f"\n=== {symbol} 交易所條件單（止盈/止損）===")
    cond = await asyncio.to_thread(client.futures_get_open_orders, symbol=symbol, conditional=True)
    _fmt_orders(cond)

    pos = await asyncio.to_thread(client.futures_position_information, symbol=symbol)
    amt = float(pos[0]["positionAmt"]) if pos else 0.0
    print(f"\n=== {symbol} 目前倉位：{amt} ===")
    if args.cleanup:
        for kwargs in ({"conditional": True}, {}):
            try:
                await asyncio.to_thread(client.futures_cancel_all_open_orders, symbol=symbol, **kwargs)
            except Exception:
                pass
        print("（已依 --cleanup 撤掉一般單與條件單）")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="幣安合約 testnet 實單測試")
    ap.add_argument("--symbol", default="BTCUSDT", help="交易對（預設 BTCUSDT）")
    ap.add_argument("--usdt", type=float, help="本筆投入 USDT（覆寫 .env 的 TRADE_USDT）")
    ap.add_argument("--entry", type=float, help="指定進場價（不給就用現價±offset 自動算）")
    ap.add_argument("--entry-offset-pct", type=float, default=0.1,
                    help="進場價相對現價的偏移%%，正=上方(易成交) 負=下方(掛等)；預設 +0.1")
    ap.add_argument("--tp-pcts", type=float, nargs="+", default=[1, 2, 3, 4],
                    help="各目標價相對進場價的漲幅%%（預設 1 2 3 4）")
    ap.add_argument("--sl-pct", type=float, default=2.0,
                    help="停損價相對進場價的跌幅%%（預設 2）")
    ap.add_argument("--wait", type=int, default=60, help="送單後觀察秒數（預設 60）")
    ap.add_argument("--tick", type=int, default=15, help="觀察期間每幾秒對帳一次（預設 15）")
    ap.add_argument("--yes", "-y", action="store_true", help="跳過送出前確認")
    ap.add_argument("--cleanup", action="store_true", help="結束時撤掉該交易對所有掛單")
    ap.add_argument("--i-know-real", action="store_true",
                    help="允許對正式網（真錢）執行；不加則 BINANCE_TESTNET=0 時拒絕")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.usdt:
        bt.TRADE_USDT = args.usdt
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已中斷。")
