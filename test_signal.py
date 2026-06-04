"""用「你實際收到的訊號文字」測試：dry-run 看下單計畫，或 --live 在 testnet 實單。

容錯解析（進場/進場價、目標價N/目標價 N、停損價、% 括號、URL 都能吃）。

    python test_signal.py --file signal.txt              # 乾跑：看會怎麼下單（零風險）
    python test_signal.py --file signal.txt --live -y    # testnet 實單（進場貼現價、立即成交）
    python test_signal.py --file signal.txt --live --use-signal-entry  # 用訊號原始進場價（可能掛著等）

不給 --file 就用內建 VELVET 範例。--live 預設只允許 testnet。
"""

import argparse
import asyncio
import re
import sys

from dotenv import load_dotenv

load_dotenv()

from binance.client import Client  # noqa: E402

import binance_trader as bt  # noqa: E402
import trades  # noqa: E402

SAMPLE = """🆕 #VELVETUSDT

24 小時成交量排名: 272th/580
市值: 42.21M

風險等級：⚠️ 較高

➡️ 進場價: 0.1162

🎯 目標價 1: 0.1196
🎯 目標價 2: 0.1230
🎯 目標價 3: 0.1333
🎯 目標價 4: 0.1504

🛑 停損價 1: 0.1082
🛑 停損價 2: 0.0888

📊 https://www.tradingview.com/chart?symbol=BINANCE%3AVELVETUSDT.P"""

_QUOTES = "USDT|USDC|USD|BUSD|FDUSD|TUSD|DAI"
_RE_SYMBOL = re.compile(rf"#?([A-Z0-9]{{2,15}}(?:{_QUOTES}))\b")
_RE_ENTRY = re.compile(r"進場價?\s*[：:]\s*([\d.]+)")
_RE_TP = re.compile(r"目標價\s*(\d+)\s*[：:]\s*([\d.]+)")
_RE_SL = re.compile(r"停損價\s*(\d+)\s*[：:]\s*([\d.]+)")
_RE_URL = re.compile(r"https?://\S+")


def parse_pasted(text: str) -> dict | None:
    m_sym = _RE_SYMBOL.search(_RE_URL.sub("", text))
    m_entry = _RE_ENTRY.search(text)
    if not (m_sym and m_entry):
        return None
    entry = float(m_entry.group(1))
    pct = lambda p: round((p - entry) / entry * 100, 2)  # noqa: E731
    targets = [{"level": int(m.group(1)), "price": float(m.group(2)), "pct": pct(float(m.group(2)))}
               for m in _RE_TP.finditer(text)]
    stops = [{"level": int(m.group(1)), "price": float(m.group(2)), "pct": pct(float(m.group(2)))}
             for m in _RE_SL.finditer(text)]
    return {"symbol": m_sym.group(1), "entry": entry, "targets": targets, "stops": stops}


def rescale(signal: dict, new_entry: float) -> dict:
    """把進場價換成 new_entry，目標/停損依原訊號的%重算（保留策略形狀）。"""
    def px(p):
        return new_entry * (1 + p / 100)
    return {
        "symbol": signal["symbol"], "entry": new_entry,
        "targets": [{**t, "price": px(t["pct"])} for t in signal["targets"]],
        "stops": [{**s, "price": px(s["pct"])} for s in signal["stops"]],
    }


def _fmt_orders(orders: list) -> None:
    if not orders:
        print("  （無）")
        return
    for o in orders:
        oid = o.get("orderId") or o.get("algoId")
        otype = o.get("type") or o.get("orderType")
        qty = o.get("origQty") or o.get("quantity")
        trig = o.get("stopPrice") or o.get("triggerPrice") or "0"
        trig_txt = f" trig={trig}" if trig and float(trig) > 0 else ""
        flags = [f for f in ("reduceOnly", "closePosition") if o.get(f)]
        tag = f" [{','.join(flags)}]" if flags else ""
        print(f"  #{oid} {o['side']} {otype} qty={qty}{trig_txt}{tag}")


def mainnet_filters(futures: bool, symbol: str) -> dict | None:
    c = Client()
    if futures:
        info = c.futures_exchange_info()
        sym = next((s for s in info["symbols"] if s["symbol"] == symbol), None)
    else:
        sym = c.get_symbol_info(symbol)
    if not sym:
        return None
    f = {x["filterType"]: x for x in sym["filters"]}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return {"step": f["LOT_SIZE"]["stepSize"], "tick": f["PRICE_FILTER"]["tickSize"],
            "min_qty": f["LOT_SIZE"]["minQty"],
            "min_notional": notional.get("minNotional") or notional.get("notional") or "0"}


def dry_run(signal: dict) -> None:
    symbol = signal["symbol"]
    market = "合約" if bt.FUTURES else "現貨"
    filt = mainnet_filters(bt.FUTURES, symbol)
    if not filt:
        print(f"❌ {symbol} 在正式網{market}找不到，無法估算")
        return
    entry = bt._quantize(signal["entry"], filt["tick"])
    n = min(len(bt.TP_RATIOS), len(signal["targets"]))
    amount = bt._min_amount(filt, float(entry), n) if bt.AUTO_MIN_AMOUNT else bt.TRADE_USDT
    amt_src = "自動最小金額" if bt.AUTO_MIN_AMOUNT else "固定 TRADE_USDT"
    qty = bt._quantize(amount / float(entry), filt["step"])
    portions = bt._split_portions(qty, n, filt["step"])
    sl1 = bt._quantize(signal["stops"][0]["price"], filt["tick"])
    print(f"\n=== 下單計畫（dry-run，正式網{market}門檻）===")
    print(f"  交易對   : {symbol}")
    print(f"  進場價   : {entry}")
    print(f"  金額     : {amt_src} → {amount:.4g} USDT")
    print(f"  買進     : {qty}（名目 {float(qty) * float(entry):.2f} USDT）")
    print(f"  門檻     : minNotional={filt['min_notional']} minQty={filt['min_qty']} step={filt['step']}")
    for i in range(n):
        t = signal["targets"][i]
        print(f"  TP{i + 1}: 賣 {portions[i]} @ {bt._quantize(t['price'], filt['tick'])}（{t['pct']:+}%）")
    print(f"  SL: 全平 @ {sl1}（{signal['stops'][0]['pct']:+}%）")
    if any(p <= 0 for p in portions):
        print("  ⚠️ 有分批為 0！開 AUTO_MIN_AMOUNT 或調高 TRADE_USDT")


async def live_run(signal: dict, args: argparse.Namespace) -> None:
    if not bt.TESTNET and not args.i_know_real:
        print("❌ 安全保護：BINANCE_TESTNET=0（真錢）。只允許 testnet，加 --i-know-real 才放行。")
        return
    if not bt.FUTURES:
        print("❌ 此實單測試目前針對合約，請 .env 設 BINANCE_FUTURES=1。")
        return
    bt.init()
    symbol = signal["symbol"]
    client = bt._client

    ticker = await asyncio.to_thread(client.futures_symbol_ticker, symbol=symbol)
    market = float(ticker.get("price") or 0)
    use_signal_entry = args.use_signal_entry
    if market <= 0:
        print(f"\n⚠️ {symbol} 在 testnet 沒有現價報價（流動性低／新上市），無法以現價進場。")
        print("   testnet 很可能無法撮合成交。想看完整鏈路（成交→止盈止損），"
              "請改用活躍幣的訊號（如 BTCUSDT / ETHUSDT）。")
        print("   這次先用訊號原始進場價送出（多半會掛著不成交）。")
        use_signal_entry = True

    if not use_signal_entry:
        new_entry = market * 1.001  # 貼現價上方一點 → 立即成交
        signal = rescale(signal, new_entry)
        print(f"\n進場改用現價 {new_entry:.6g}（保證成交；目標/停損依原訊號%重算）")
    else:
        note = f"（現價 {market}）" if market > 0 else "（testnet 無現價）"
        tail = "，低於市價會掛著等成交" if 0 < market and signal["entry"] < market else ""
        print(f"\n用訊號原始進場價 {signal['entry']}{note}{tail}")

    print(f"目標 {[round(t['price'], 6) for t in signal['targets']]} | "
          f"停損 {round(signal['stops'][0]['price'], 6)}")
    if not args.yes and input("\n確定在 testnet 送出？(y/N) ").strip().lower() != "y":
        print("已取消。")
        return

    await bt.on_signal(signal)
    print(f"\n等待 {args.wait}s 觀察成交／掛單／對帳…")
    elapsed = 0
    while elapsed < args.wait:
        await asyncio.sleep(min(args.tick, args.wait - elapsed))
        elapsed += args.tick
        for t in trades.list_status("ACTIVE"):
            if t["symbol"] == symbol:
                await bt._reconcile(t)

    print("\n=== 本地交易紀錄 ===")
    for st in ("PENDING_BUY", "ACTIVE", "CLOSED", "CANCELED"):
        for t in trades.list_status(st):
            if t["symbol"] == symbol:
                print(f"  trade#{t['id']} status={t['status']} entry={t['entry']} "
                      f"qty={t['qty']} sl_moved={t.get('sl_moved')}")
    print(f"\n=== {symbol} 一般掛單 ===")
    _fmt_orders(await asyncio.to_thread(client.futures_get_open_orders, symbol=symbol))
    print(f"=== {symbol} 條件單（止盈/止損）===")
    _fmt_orders(await asyncio.to_thread(client.futures_get_open_orders, symbol=symbol, conditional=True))
    pos = await asyncio.to_thread(client.futures_position_information, symbol=symbol)
    print(f"=== {symbol} 倉位：{float(pos[0]['positionAmt']) if pos else 0} ===")

    if args.cleanup:
        for kw in ({"conditional": True}, {}):
            try:
                await asyncio.to_thread(client.futures_cancel_all_open_orders, symbol=symbol, **kw)
            except Exception:
                pass
        print("（已依 --cleanup 撤掉一般單與條件單；倉位如有需自行平倉）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="訊號文字檔（不給用內建 VELVET 範例）")
    ap.add_argument("--live", action="store_true", help="在 testnet 實單（預設只 dry-run）")
    ap.add_argument("--use-signal-entry", action="store_true", help="實單時用訊號原始進場價（預設貼現價立即成交）")
    ap.add_argument("--wait", type=int, default=90, help="實單後觀察秒數（預設 90）")
    ap.add_argument("--tick", type=int, default=15, help="觀察期間每幾秒對帳一次")
    ap.add_argument("--cleanup", action="store_true", help="結束撤掉該交易對掛單")
    ap.add_argument("--yes", "-y", action="store_true", help="跳過送出確認")
    ap.add_argument("--i-know-real", action="store_true", help="允許對正式網執行（後果自負）")
    args = ap.parse_args()

    text = open(args.file, encoding="utf-8").read() if args.file else SAMPLE
    signal = parse_pasted(text)
    if not signal:
        print("❌ 解析不出訊號（缺幣別或進場價）")
        sys.exit(1)
    print(f"解析：{signal['symbol']} 進場 {signal['entry']} | "
          f"{len(signal['targets'])} 目標 / {len(signal['stops'])} 停損")

    if args.live:
        asyncio.run(live_run(signal, args))
    else:
        dry_run(signal)


if __name__ == "__main__":
    main()
