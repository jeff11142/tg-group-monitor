"""列出幣安帳戶目前的掛單與餘額（預設讀 .env 的 testnet 設定）。

用法：
    python show_orders.py                # 列 BTCUSDT 掛單 + 主要餘額
    python show_orders.py --symbol ETHUSDT
"""

import argparse

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT", help="要查的交易對")
    args = ap.parse_args()
    symbol = args.symbol.upper()

    import binance_trader as trader

    trader.init()
    client = trader._client

    orders = client.get_open_orders(symbol=symbol)
    print(f"\n=== {symbol} 未結掛單：{len(orders)} 筆 ===")
    for o in sorted(orders, key=lambda x: x["orderId"]):
        stop = o.get("stopPrice", "0")
        stop_txt = f" stop={stop}" if stop and float(stop) > 0 else ""
        print(f"#{o['orderId']} {o['side']} {o['type']} qty={o['origQty']} "
              f"price={o['price']}{stop_txt} listId={o.get('orderListId')}")

    base = symbol.replace("USDT", "").replace("USDC", "")
    for asset in (base, "USDT"):
        bal = client.get_asset_balance(asset=asset)
        if bal:
            print(f"餘額 {asset}: free={bal['free']} locked={bal['locked']}")


if __name__ == "__main__":
    main()
