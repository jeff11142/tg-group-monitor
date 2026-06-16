"""CM_MacD_Ult_MTF 指標 → 策略回測（BTCUSDT 1H 合約）。

指標還原（與 Pine 完全一致）：
  macd = ema(close,fast) - ema(close,slow)；signal = SMA(macd, sigLen)（CM 版用 SMA！）
  hist = macd - signal

策略變體（從指標元素衍生）：
  R1 交叉做多：MACD 上穿 signal 進多，下穿出場（= 指標的圓點訊號）
  R2 交叉多空：上穿做多、下穿翻空，永遠在場
  R3 零軸趨勢濾網：只有 MACD>0 時的上穿才進多（順勢）
  R4 零軸抄底：只有 MACD<0 時的上穿才進多，下穿出場（逆勢）
  R5 柱狀圖轉折：hist<0 且翻紅(histB_IsUp)進多、hist>0 且轉弱(histA_IsDown)出場

執行假設：訊號收盤確認、下一根開盤價成交（無未來函數）；taker 0.05%/邊。
驗證：訓練 2019-09~2023-12 選參數 → 驗證 2024-01~2026-06 看真實成績；
      鄰近參數穩定性 + 逐年分解，防參數孤峰。
"""
import json
from datetime import datetime, timezone

FEE = 0.0005          # taker 單邊
SPLIT_MS = 1704067200000   # 2024-01-01：訓練/驗證分界

CANDLES = json.load(open("btc_1h.json"))
TS = [c[0] for c in CANDLES]
O = [c[1] for c in CANDLES]
C = [c[4] for c in CANDLES]
N = len(CANDLES)
SPLIT_I = next(i for i, t in enumerate(TS) if t >= SPLIT_MS)


def ema(src: list, length: int) -> list:
    a = 2 / (length + 1)
    out = [src[0]]
    for x in src[1:]:
        out.append(out[-1] + a * (x - out[-1]))
    return out


def sma(src: list, length: int) -> list:
    out, s = [], 0.0
    for i, x in enumerate(src):
        s += x
        if i >= length:
            s -= src[i - length]
        out.append(s / min(i + 1, length))
    return out


def positions(macd: list, sig: list, variant: str) -> list:
    """每根收盤後的目標倉位（+1/0/-1），下一根開盤執行。"""
    pos, out = 0, [0] * N
    for i in range(1, N):
        up = macd[i] > sig[i] and macd[i - 1] <= sig[i - 1]
        dn = macd[i] < sig[i] and macd[i - 1] >= sig[i - 1]
        if variant == "R1":
            pos = 1 if macd[i] > sig[i] else 0
        elif variant == "R2":
            pos = 1 if macd[i] > sig[i] else -1
        elif variant == "R3":
            if up and macd[i] > 0:
                pos = 1
            elif dn:
                pos = 0
        elif variant == "R4":
            if up and macd[i] < 0:
                pos = 1
            elif dn:
                pos = 0
        elif variant == "R5":
            h0, h1 = macd[i] - sig[i], macd[i - 1] - sig[i - 1]
            if h0 > h1 and h0 <= 0:
                pos = 1
            elif h0 < h1 and h0 > 0:
                pos = 0
        elif variant == "R6":               # 零軸濾網雙向：順勢多 + 順勢空
            if up and macd[i] > 0:
                pos = 1
            elif dn and macd[i] < 0:
                pos = -1
            elif (dn and pos == 1) or (up and pos == -1):
                pos = 0
        out[i] = pos
    return out


def run(pos: list, i0: int, i1: int) -> dict:
    """在 [i0,i1) 區間模擬：倉位變化於下一根開盤成交。回傳績效。"""
    eq, peak, dd = 1.0, 1.0, 0.0
    cur = 0
    entry_eq = 1.0
    trades = []
    for i in range(i0, i1 - 1):
        nxt = pos[i]
        if nxt != cur:                      # 下一根開盤調倉
            px = O[i + 1]
            eq *= (1 - FEE * abs(nxt - cur))    # 翻倉收兩邊手續費
            if cur != 0:
                trades.append(eq / entry_eq - 1)
            if nxt != 0:
                entry_eq = eq
            cur = nxt
        if cur != 0 and i + 2 < N:          # 持倉损益：下一根開到再下一根開
            eq *= 1 + cur * (O[i + 2] / O[i + 1] - 1)
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
    if cur != 0:
        trades.append(eq / entry_eq - 1)
    wins = [t for t in trades if t > 0]
    loss = [t for t in trades if t <= 0]
    pf = (sum(wins) / -sum(loss)) if loss and sum(loss) < 0 else float("inf")
    return {"ret": eq - 1, "maxdd": dd, "n": len(trades),
            "win": len(wins) / len(trades) if trades else 0, "pf": pf}


def macd_arrays(fast: int, slow: int, sig_len: int):
    f, s = ema(C, fast), ema(C, slow)
    macd = [a - b for a, b in zip(f, s)]
    return macd, sma(macd, sig_len)


def main() -> None:
    grid_f = [5, 8, 10, 12, 16, 20]
    grid_s = [21, 26, 35, 50, 60]
    grid_g = [5, 7, 9, 12, 15, 18, 22, 26, 30, 40]
    variants = ["R1", "R2", "R3", "R4", "R5", "R6"]
    results = []
    for f in grid_f:
        for s in grid_s:
            if f >= s:
                continue
            for g in grid_g:
                macd, sig = macd_arrays(f, s, g)
                for v in variants:
                    pos = positions(macd, sig, v)
                    tr = run(pos, 200, SPLIT_I)          # 前 200 根暖機
                    results.append({"v": v, "f": f, "s": s, "g": g, "train": tr})
    json.dump(results, open("macd_grid.json", "w"))

    # 訓練期排名（用 報酬/回撤 比，鼓勵平穩而非孤注）
    def score(r):
        t = r["train"]
        return t["ret"] / max(0.05, -t["maxdd"]) if t["n"] >= 30 else -99
    results.sort(key=score, reverse=True)

    bh_train = C[SPLIT_I] / O[201] - 1
    bh_test = C[-1] / O[SPLIT_I + 1] - 1
    print(f"訓練期 B&H: {bh_train*100:+.1f}% | 驗證期 B&H: {bh_test*100:+.1f}%")
    print(f"\n訓練期 Top 10（共 {len(results)} 組合）→ 直接看驗證期是否守住：")
    print(f"{'變體':<4}{'fast':>5}{'slow':>5}{'sig':>4} | {'訓練報酬':>9}{'回撤':>8}{'筆':>5}{'勝率':>6}{'PF':>6} | {'驗證報酬':>9}{'回撤':>8}{'筆':>5}{'勝率':>6}{'PF':>6}")
    for r in results[:10]:
        macd, sig = macd_arrays(r["f"], r["s"], r["g"])
        pos = positions(macd, sig, r["v"])
        te = run(pos, SPLIT_I, N)
        r["test"] = te
        t = r["train"]
        print(f"{r['v']:<4}{r['f']:>5}{r['s']:>5}{r['g']:>4} | "
              f"{t['ret']*100:>+8.1f}%{t['maxdd']*100:>7.1f}%{t['n']:>5}{t['win']*100:>5.0f}%{min(t['pf'],99):>6.2f} | "
              f"{te['ret']*100:>+8.1f}%{te['maxdd']*100:>7.1f}%{te['n']:>5}{te['win']*100:>5.0f}%{min(te['pf'],99):>6.2f}")

    # 各變體最佳組的逐年表現（含驗證期）
    best = results[0]
    macd, sig = macd_arrays(best["f"], best["s"], best["g"])
    pos = positions(macd, sig, best["v"])
    print(f"\n冠軍組 {best['v']} f={best['f']} s={best['s']} sig={best['g']} 逐年：")
    years = {}
    for i, t in enumerate(TS):
        years.setdefault(datetime.fromtimestamp(t / 1000, tz=timezone.utc).year, [i, i])[1] = i
    for y, (i0, i1) in sorted(years.items()):
        r = run(pos, max(i0, 200), i1)
        bh = C[i1] / O[max(i0, 200) + 1] - 1
        print(f"  {y}: 策略 {r['ret']*100:+7.1f}%（回撤 {r['maxdd']*100:.1f}%, {r['n']}筆） vs B&H {bh*100:+7.1f}%")


if __name__ == "__main__":
    main()
