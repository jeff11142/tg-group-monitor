"""回測：signals_history.jsonl + Binance 合約 5m K 線，比較多種止損策略的期望值。

共同規則（對齊 binance_trader.py 線上行為）：
- 進場：限價單掛訊號進場價，30 分內 low<=entry 才成交；成交前先碰到 TP1 就撤單
- 分批止盈 TP_RATIOS=30/30/20/20（限價 maker 0.02%）；止損市價 taker 0.05%
- 同根 K 線同時碰 TP 與 SL：悲觀（SL 先）/ 樂觀（TP 先）各算一次，回報區間
- 不計資金費率；超過 ~10 天未了結記 OPEN，剩餘部位以最後收盤價結算

情境（SL 策略）：
  A 單一SL1固定不動（現行線上行為）
  B 訊號源 SL1+SL2 各半倉、固定不動
  C 訊號源 SL1+SL2 雙軌、隨 TP 階梯上移（上軌=rung[tier+1]、下軌=rung[tier]）
  D 單一SL1、隨 TP 階梯上移（TP1→保本、TP2→TP1、TP3→TP2）
  E 單一SL1、TP1 後移到開倉價（淨保本）之後不再動
  F 單一SL1、落後一階上移（TP2→保本、TP3→TP1）：多給回踩空間
  G 只掛 SL2（約-15.4%）固定：寬災難止損
  H 單一SL1固定 + 24小時未到 TP1 就市價平倉（時間停損）

用法：
    .venv/bin/python backtest.py                 # 全量（可中斷續跑）
    .venv/bin/python backtest.py --limit 200     # 先抽最近 200 筆試跑
    .venv/bin/python backtest.py --report-only   # 只用既有結果重印報告
"""
import argparse
import asyncio
import json
import os
from collections import Counter
from datetime import datetime

import aiohttp

SIGNALS_FILE = "signals_history.jsonl"
RESULTS_FILE = "backtest_results.jsonl"

FAPI = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL = "5m"
CANDLE_MS = 5 * 60 * 1000
BATCH = 1000                    # 每次抓 1000 根（權重 5）
MAX_BATCHES = 3                 # 最多 3000 根 ≈ 10.4 天
ENTRY_TIMEOUT_MIN = 30
SL1_CAP_PCT = 5.0               # SL1 = min(訊號SL1距離, 此上限)
SL2_FALLBACK_PCT = 15.39        # 訊號缺 SL2 時用模板值
TP_RATIOS = [30, 30, 20, 20]
BE_FEE = 0.001                  # 淨保本 = entry ×(1+0.1%)
MAKER, TAKER = 0.0002, 0.0005
EPS = 1e-9

SCENARIOS = {
    "A 單一SL1固定(現行)":      dict(dual=False, trail="none"),
    "B 雙軌SL1+SL2固定":        dict(dual=True,  trail="none"),
    "C 雙軌SL1+SL2階梯上移":    dict(dual=True,  trail="full"),
    "D 單一SL1階梯上移":        dict(dual=False, trail="full"),
    "E 單一SL1→TP1後保本鎖死":  dict(dual=False, trail="be_once"),
    "F 單一SL1落後一階上移":    dict(dual=False, trail="lazy"),
    "G 只掛SL2寬災難止損":      dict(dual=False, trail="none", use_sl2=True),
    "H 固定SL1+24h時間停損":    dict(dual=False, trail="none", time_stop_h=24),
}


def _levels(sig: dict) -> dict | None:
    """訊號 → 各價位。tps 不足 4 段回 None。"""
    entry = sig["entry"]
    tps = [t["price"] for t in sorted(sig["targets"], key=lambda t: t["level"])]
    if len(tps) < 4:
        return None
    stops = sorted(sig.get("stops") or [], key=lambda s: s["level"])
    sl_cap = entry * (1 - SL1_CAP_PCT / 100)
    sl1 = max(stops[0]["price"], sl_cap) if stops else sl_cap     # 距離取小=價格取高（做多）
    sl2 = stops[1]["price"] if len(stops) > 1 else entry * (1 - SL2_FALLBACK_PCT / 100)
    return {"entry": entry, "tps": tps, "sl1": sl1, "sl2": sl2, "be": entry * (1 + BE_FEE)}


def _stop_legs(lv: dict, sc: dict, tier: int, rem: float) -> list:
    """目前 tier 之下該掛哪些止損腿：[[價格, 部位比例(佔原始倉位)], ...]"""
    tps = lv["tps"]
    if sc["dual"]:
        rungs = [lv["sl2"], lv["sl1"], lv["be"], tps[0], tps[1]]
        if sc["trail"] == "full":
            up, lo = rungs[min(tier + 1, 4)], rungs[min(tier, 3)]
        else:
            up, lo = lv["sl1"], lv["sl2"]
        return [[up, rem / 2], [lo, rem / 2]]
    if sc.get("use_sl2"):
        p = lv["sl2"]
    elif sc["trail"] == "full":
        p = [lv["sl1"], lv["be"], tps[0], tps[1]][min(tier, 3)]
    elif sc["trail"] == "be_once":
        p = lv["sl1"] if tier == 0 else lv["be"]
    elif sc["trail"] == "lazy":
        p = [lv["sl1"], lv["sl1"], lv["be"], tps[0]][min(tier, 3)]
    else:                                   # none
        p = lv["sl1"]
    return [[p, rem]]


def simulate(sig: dict, candles: list, pessimistic: bool, sc: dict) -> dict | None:
    """單筆單情境模擬。回傳 {filled, cancelled, tiers, pnl, kind}。pnl=全倉名目%（含手續費）。"""
    lv = _levels(sig)
    if lv is None:
        return None
    entry, tps = lv["entry"], lv["tps"]
    w = [r / sum(TP_RATIOS) for r in TP_RATIOS]

    # --- 進場窗：30 分鐘內 low<=entry 才成交；先碰 TP1 就撤 ---
    t0 = candles[0][0]
    fill_i = None
    for i, (ts, o, h, l, c) in enumerate(candles):
        if ts - t0 > ENTRY_TIMEOUT_MIN * 60 * 1000:
            return {"filled": False, "cancelled": False}
        if l <= entry:
            fill_i = i
            break
        if h >= tps[0]:
            return {"filled": False, "cancelled": True}
    if fill_i is None:
        return {"filled": False, "cancelled": False}

    tier, rem = 0, 1.0
    pnl = -MAKER * 100                                  # 進場手續費（全倉名目）
    legs = _stop_legs(lv, sc, 0, 1.0)
    fill_ts = candles[fill_i][0]
    kind = None

    def hit_stops(low: float) -> None:
        nonlocal rem, pnl
        for leg in legs:
            if leg[1] > EPS and low <= leg[0]:
                q = min(leg[1], rem)
                pnl += q * ((leg[0] / entry - 1) * 100 - TAKER * 100)
                rem -= q
                leg[1] = 0

    def hit_tps(high: float) -> bool:
        nonlocal tier, rem, pnl
        moved = False
        while tier < 4 and high >= tps[tier] and rem > EPS:
            q = min(w[tier], rem)
            pnl += q * ((tps[tier] / entry - 1) * 100 - MAKER * 100)
            rem -= q
            tier += 1
            moved = True
        return moved

    # 成交當根：盤中順序未知。悲觀=止損同根也可能被掃；樂觀=這根不處理
    if pessimistic:
        hit_stops(candles[fill_i][3])

    for i in range(fill_i + 1, len(candles)):
        if rem <= EPS:
            break
        ts, o, h, l, c = candles[i]
        if sc.get("time_stop_h") and tier == 0 and ts - fill_ts >= sc["time_stop_h"] * 3600 * 1000:
            pnl += rem * ((o / entry - 1) * 100 - TAKER * 100)   # 開盤價市價平倉
            rem, kind = 0.0, "TIME"
            break
        if pessimistic:
            hit_stops(l)
            if rem <= EPS:
                break
            if hit_tps(h):
                legs = _stop_legs(lv, sc, tier, rem)
                hit_stops(l)                 # 上移後的新止損同根被掃，最壞情況也算
        else:
            if hit_tps(h):
                legs = _stop_legs(lv, sc, tier, rem)
            hit_stops(l)

    if rem > EPS:                                       # 未了結：以最後收盤價結算剩餘
        pnl += rem * ((candles[-1][4] / entry - 1) * 100 - MAKER * 100)
        kind = "OPEN"
    elif kind is None:
        kind = "TP4" if tier == 4 else "SL"
    return {"filled": True, "tiers": tier, "pnl": round(pnl, 4), "kind": kind}


async def fetch_candles(session: aiohttp.ClientSession, symbol: str, start_ms: int,
                        sem: asyncio.Semaphore) -> list | None:
    """抓最多 MAX_BATCHES×1000 根 5m K 線；symbol 不存在回 None。"""
    out, cursor = [], start_ms
    for _ in range(MAX_BATCHES):
        async with sem:
            for attempt in range(5):
                async with session.get(FAPI, params={
                        "symbol": symbol, "interval": INTERVAL,
                        "startTime": cursor, "limit": BATCH}) as r:
                    if r.status in (429, 418):
                        await asyncio.sleep(60)
                        continue
                    data = await r.json()
                    if r.status != 200:
                        if isinstance(data, dict) and data.get("code") == -1121:
                            return None
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", 0))
                    if used > 1800:
                        await asyncio.sleep(15)
                    break
            else:
                raise RuntimeError(f"{symbol} K線抓取連續失敗")
        if not data:
            break
        out.extend([[k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in data])
        if len(data) < BATCH:
            break
        cursor = data[-1][0] + CANDLE_MS
        if out and _range_resolved(out):
            break
    return out


def _range_resolved(candles: list) -> bool:
    """價格已同時涵蓋 +20%/-16%（所有情境必然了結）就不用再抓。"""
    first_open = candles[0][1]
    hi = max(c[2] for c in candles)
    lo = min(c[3] for c in candles)
    return hi >= first_open * 1.20 or lo <= first_open * 0.84


async def run(limit: int | None) -> None:
    signals = [json.loads(line) for line in open(SIGNALS_FILE, encoding="utf-8")]
    signals.sort(key=lambda s: s["date"])
    if limit:
        signals = signals[-limit:]
    done = set()
    if os.path.exists(RESULTS_FILE):
        done = {json.loads(line)["msg_id"] for line in open(RESULTS_FILE, encoding="utf-8")}
    todo = [s for s in signals if s["msg_id"] not in done]
    print(f"訊號 {len(signals)} 筆，已完成 {len(done)}，本次要跑 {len(todo)}，情境 {len(SCENARIOS)} 個")

    sem = asyncio.Semaphore(6)
    out_f = open(RESULTS_FILE, "a", encoding="utf-8")
    count = 0

    async def one(sig: dict) -> None:
        nonlocal count
        start_ms = int(datetime.fromisoformat(sig["date"]).timestamp() * 1000)
        start_ms -= start_ms % CANDLE_MS
        candles = await fetch_candles(session, sig["symbol"].lstrip("#"), start_ms, sem)
        row = {"msg_id": sig["msg_id"], "symbol": sig["symbol"], "date": sig["date"],
               "risk": sig.get("risk")}
        if candles is None:
            row["skip"] = "no_futures_symbol"
        elif not candles:
            row["skip"] = "no_kline_data"
        elif _levels(sig) is None:
            row["skip"] = "bad_signal"
        else:
            row["res"] = {name: {"pess": simulate(sig, candles, True, sc),
                                 "opt": simulate(sig, candles, False, sc)}
                          for name, sc in SCENARIOS.items()}
        out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        count += 1
        if count % 100 == 0:
            out_f.flush()
            print(f"  進度 {count}/{len(todo)}")

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(s) for s in todo))
    out_f.close()
    print("資料蒐集完成")


def report() -> None:
    rows = [json.loads(line) for line in open(RESULTS_FILE, encoding="utf-8")]
    rows = list({r["msg_id"]: r for r in rows}.values())
    skipped = Counter(r["skip"] for r in rows if r.get("skip"))
    sims = [r for r in rows if r.get("res")]
    print(f"\n{'='*100}")
    print(f"回測報告：共 {len(rows)} 筆訊號（可模擬 {len(sims)}；跳過 {dict(skipped) or 0}）")
    first = next(iter(SCENARIOS))
    fills = [r for r in sims if r["res"][first]["pess"]["filled"]]
    cancelled = sum(1 for r in sims if r["res"][first]["pess"].get("cancelled"))
    print(f"進場成交 {len(fills)} | 先到TP1撤單 {cancelled} | 超時未成交 {len(sims)-len(fills)-cancelled}")
    print(f"\n{'情境':<26}{'EV悲觀%':>9}{'EV樂觀%':>9}{'30U本金悲觀':>12}"
          f"{'直掃SL%':>9}{'TP4%':>7}{'OPEN%':>7}  分佈 t0/t1/t2/t3/t4")
    for name in SCENARIOS:
        stats = {}
        for mode in ("pess", "opt"):
            ps = [r["res"][name][mode] for r in fills]
            stats[mode] = sum(p["pnl"] for p in ps) / len(ps)
            if mode == "pess":
                tiers = Counter(p["tiers"] for p in ps)
                kinds = Counter(p["kind"] for p in ps)
                n = len(ps)
                dist = "/".join(f"{tiers.get(k,0)/n*100:.0f}" for k in range(5))
                straight = sum(1 for p in ps if p["tiers"] == 0 and p["kind"] in ("SL", "TIME")) / n * 100
                tp4 = kinds.get("TP4", 0) / n * 100
                open_ = kinds.get("OPEN", 0) / n * 100
        # 30U 本金 ×5 槓桿 → 名目 150U；本金報酬% = EV% × 5
        print(f"{name:<26}{stats['pess']:>+9.3f}{stats['opt']:>+9.3f}"
              f"{stats['pess']*5:>+11.2f}%{straight:>8.1f}{tp4:>7.1f}{open_:>7.1f}  {dist}")
    print(f"{'='*100}")
    print("EV=每筆全倉名目損益%（含手續費，不含資金費率）；30U本金欄=本金報酬%（×5槓桿）")
    print("悲觀/樂觀=同根5分K同時碰TP與SL時的兩種極端假設，真實值在兩者之間")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只跑最近 N 筆")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    if not args.report_only:
        asyncio.run(run(args.limit))
    report()
