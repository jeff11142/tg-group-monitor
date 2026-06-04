"""查詢交易對的最小下單資訊（最小數量／金額、步進），並換算建議的 TRADE_USDT。

依 .env 的 BINANCE_FUTURES（現貨/合約）與 BINANCE_TESTNET 設定查詢。

用法：
    python check_symbol.py                 # 預設 BTCUSDT
    python check_symbol.py ETHUSDT SOLUSDT # 一次查多個
"""

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

import binance_trader as bt  # noqa: E402（load_dotenv 要先跑）


async def run(symbols: list[str]) -> None:
    bt.init()
    market = "合約" if bt.FUTURES else "現貨"
    for sym in symbols:
        f = await bt._get_filters(sym)
        if not f:
            print(f"\n{sym}（{market}）: 找不到此交易對（可能不支援）")
            continue
        mn = float(f["min_notional"])
        print(f"\n=== {sym}（{market}）===")
        print(f"  最小數量 minQty      : {f['min_qty']}")
        print(f"  數量步進 stepSize    : {f['step']}")
        print(f"  價格步進 tickSize    : {f['tick']}")
        print(f"  最小名目金額 minNotional : {mn:g} USDT")
        if bt.FUTURES:
            print(f"  → 合約 {bt.LEVERAGE}x：每筆名目 ≥ {mn:g} USDT"
                  f"（1x 時 TRADE_USDT 至少 ≈ {mn:g}）")
        else:
            smallest = min(bt.TP_RATIOS) / sum(bt.TP_RATIOS)
            need = mn / smallest
            print(f"  → 現貨：拆 {bt.TP_RATIOS} 後最小一份要 ≥ {mn:g}，"
                  f"故 TRADE_USDT 至少 ≈ {need:.0f}")


def main() -> None:
    symbols = [s.upper() for s in sys.argv[1:]] or ["BTCUSDT"]
    asyncio.run(run(symbols))


if __name__ == "__main__":
    main()
