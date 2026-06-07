"""查詢交易對的「正式網（mainnet）」最小下單資訊，並換算建議的 TRADE_USDT。

⚠️ 永遠查正式網，不受 .env 的 BINANCE_TESTNET 影響（交易所資訊是公開端點、免金鑰）。
   現貨/合約則依 .env 的 BINANCE_FUTURES 決定要查哪個市場。

用法：
    python check_symbol.py                 # 預設 BTCUSDT
    python check_symbol.py ETHUSDT SOLUSDT # 一次查多個
"""

import sys

from dotenv import load_dotenv

load_dotenv()

from binance.client import Client  # noqa: E402

import binance_trader as bt  # noqa: E402（只讀 FUTURES / TP_RATIOS / LEVERAGE 設定，不連線）


def get_filters(client: Client, futures: bool, symbol: str) -> dict | None:
    if futures:
        info = client.futures_exchange_info()
        sym = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    else:
        sym = client.get_symbol_info(symbol)
    if not sym:
        return None
    f = {x["filterType"]: x for x in sym["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    mn = notional.get("minNotional") or notional.get("notional") or "0"
    return {
        "step": f["LOT_SIZE"]["stepSize"],
        "tick": f["PRICE_FILTER"]["tickSize"],
        "min_qty": f["LOT_SIZE"]["minQty"],
        "min_notional": mn,
    }


def main() -> None:
    symbols = [s.upper() for s in sys.argv[1:]] or ["BTCUSDT"]
    client = Client()  # 正式網公開端點，免金鑰
    futures = bt.FUTURES
    market = "合約" if futures else "現貨"
    print(f"（查詢正式網 mainnet | {market}市場）")

    for sym in symbols:
        f = get_filters(client, futures, sym)
        if not f:
            print(f"\n{sym}: 找不到此交易對（{market}市場可能不支援）")
            continue
        mn = float(f["min_notional"])
        print(f"\n=== {sym}（正式網{market}）===")
        print(f"  最小數量 minQty      : {f['min_qty']}")
        print(f"  數量步進 stepSize    : {f['step']}")
        print(f"  價格步進 tickSize    : {f['tick']}")
        print(f"  最小名目金額 minNotional : {mn:g} USDT")
        if futures:
            notional = bt.MARGIN_USDT * bt.LEVERAGE if bt.MARGIN_USDT > 0 else 0
            if notional:
                ok = notional >= mn
                verdict = "✅ 可下單" if ok else f"🔴 跳過（名目 {notional:g} < minNotional {mn:g}）"
                print(f"  → 合約 固定本金 {bt.MARGIN_USDT:g} × {bt.LEVERAGE}x"
                      f" = 名目 {notional:g} USDT → {verdict}")
            else:
                print(f"  → 合約 {bt.LEVERAGE}x：每筆名目 ≥ {mn:g} USDT"
                      f"（1x 時 TRADE_USDT 至少 ≈ {mn:g}）")
        else:
            smallest = min(bt.TP_RATIOS) / sum(bt.TP_RATIOS)
            print(f"  → 現貨：拆 {bt.TP_RATIOS} 後最小一份要 ≥ {mn:g}，"
                  f"故 TRADE_USDT 至少 ≈ {mn / smallest:.0f}")


if __name__ == "__main__":
    main()
