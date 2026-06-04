"""用「你實際收到的訊號文字」測試下單計畫。

容錯解析（進場/進場價、目標價N/目標價 N、停損價、% 括號都能吃），預設用內建的
VELVET 範例；可用 --file 讀你貼進檔案的訊號。

預設 dry-run：只查交易對最小門檻、算出「會用多少錢、買多少、4 段止盈、止損在哪」，
不碰 API、不需金鑰。加 --live 才會真的在 testnet 下單（需 .env 設好合約 testnet）。

用法：
    python test_signal.py                      # 用內建 VELVET 範例乾跑
    python test_signal.py --file signal.txt    # 讀你貼的訊號乾跑
    python test_signal.py --live               # 真的在 testnet 下單
"""

import argparse
import re
import sys
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from binance.client import Client  # noqa: E402

import binance_trader as bt  # noqa: E402

SAMPLE = """⏰ 2026-06-04 23:35:06
📊 幣別: #VELVETUSDT
💡 風險: ⚠️ 較高
📈 24h成交量: 272/580 | 市值: 42.21M

➡️ 進場: 0.1162
🎯 目標價1: 0.1196 (+2.93%)
🎯 目標價2: 0.123 (+5.85%)
🎯 目標價3: 0.1333 (+14.72%)
🎯 目標價4: 0.1504 (+29.43%)

⛔ 停損價1: 0.1082 (-6.88%)
⛔ 停損價2: 0.0888 (-23.58%)

🔗 https://www.tradingview.com/chart?symbol=BINANCE%3AVELVETUSDT.P"""

_QUOTES = "USDT|USDC|USD|BUSD|FDUSD|TUSD|DAI"
_RE_SYMBOL = re.compile(rf"#?([A-Z0-9]{{2,15}}(?:{_QUOTES}))\b")
_RE_ENTRY = re.compile(r"進場價?\s*[：:]\s*([\d.]+)")            # 進場 或 進場價
_RE_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)")
_RE_SL = re.compile(r"停損價\s*(\d+)\s*[：:]\s*([\d.]+)")
_RE_URL = re.compile(r"https?://\S+")


def parse_pasted(text: str) -> dict | None:
    """容錯解析貼上的訊號（輸出或來源格式都能吃）。"""
    m_sym = _RE_SYMBOL.search(_RE_URL.sub("", text))
    m_entry = _RE_ENTRY.search(text)
    if not (m_sym and m_entry):
        return None
    entry = float(m_entry.group(1))
    targets = [{"level": int(m.group(1)), "price": float(m.group(2)),
                "pct": round((float(m.group(2)) - entry) / entry * 100, 2)}
               for m in _RE_TP.finditer(text)]
    stops = [{"level": int(m.group(1)), "price": float(m.group(2)),
              "pct": round((float(m.group(2)) - entry) / entry * 100, 2)}
             for m in _RE_SL.finditer(text)]
    return {"symbol": m_sym.group(1), "entry": entry, "targets": targets, "stops": stops}


def mainnet_filters(futures: bool, symbol: str) -> dict | None:
    c = Client()  # 公開端點免金鑰
    if futures:
        info = c.futures_exchange_info()
        sym = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    else:
        sym = c.get_symbol_info(symbol)
    if not sym:
        return None
    f = {x["filterType"]: x for x in sym["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return {
        "step": f["LOT_SIZE"]["stepSize"],
        "tick": f["PRICE_FILTER"]["tickSize"],
        "min_qty": f["LOT_SIZE"]["minQty"],
        "min_notional": notional.get("minNotional") or notional.get("notional") or "0",
    }


def dry_run(signal: dict) -> None:
    symbol = signal["symbol"]
    market = "合約" if bt.FUTURES else "現貨"
    filt = mainnet_filters(bt.FUTURES, symbol)
    if not filt:
        print(f"❌ {symbol} 在正式網{market}找不到，無法估算")
        return

    entry = bt._quantize(signal["entry"], filt["tick"])
    n = min(len(bt.TP_RATIOS), len(signal["targets"]))
    if bt.AUTO_MIN_AMOUNT:
        amount = bt._min_amount(filt, float(entry), n)
        amt_src = "自動最小金額"
    else:
        amount = bt.TRADE_USDT
        amt_src = "固定 TRADE_USDT"
    qty = bt._quantize(amount / float(entry), filt["step"])
    portions = bt._split_portions(qty, n, filt["step"])
    sl1 = bt._quantize(signal["stops"][0]["price"], filt["tick"])

    print(f"\n=== 下單計畫（dry-run，正式網{market}門檻）===")
    print(f"  交易對     : {symbol}")
    print(f"  進場價     : {entry}")
    print(f"  金額來源   : {amt_src} → {amount:.4g} USDT")
    print(f"  買進數量   : {qty}（名目 {float(qty) * float(entry):.2f} USDT）")
    print(f"  門檻       : minNotional={filt['min_notional']} minQty={filt['min_qty']} step={filt['step']}")
    print("  ---- 分批止盈 ----")
    for i in range(n):
        t = signal["targets"][i]
        print(f"  TP{i + 1}: 賣 {portions[i]} @ {bt._quantize(t['price'], filt['tick'])}"
              f"（{t['pct']:+}%）")
    print(f"  ---- 止損 ----")
    print(f"  SL: 全平 @ {sl1}（{signal['stops'][0]['pct']:+}%）")
    if any(p <= 0 for p in portions):
        print("  ⚠️ 有分批為 0！金額太小，建議開 AUTO_MIN_AMOUNT 或調高 TRADE_USDT")


async def live_run(signal: dict) -> None:
    import asyncio  # noqa
    if not bt.TESTNET:
        print("❌ 安全保護：BINANCE_TESTNET=0（真錢），本腳本只允許 testnet。")
        return
    bt.init()
    await bt.on_signal(signal)
    print("已送出訊號，請用 test_futures_live.py 觀察或到交易所查看。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="讀取訊號文字檔（不給就用內建 VELVET 範例）")
    ap.add_argument("--live", action="store_true", help="真的在 testnet 下單（預設只 dry-run）")
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8").read() if args.file else SAMPLE
    signal = parse_pasted(text)
    if not signal:
        print("❌ 解析不出訊號（缺幣別或進場價）")
        sys.exit(1)

    print(f"解析結果：{signal['symbol']} 進場 {signal['entry']} | "
          f"{len(signal['targets'])} 目標 / {len(signal['stops'])} 停損")

    if args.live:
        import asyncio
        asyncio.run(live_run(signal))
    else:
        dry_run(signal)


if __name__ == "__main__":
    main()
