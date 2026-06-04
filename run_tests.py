"""一鍵跑完整測試套件。

預設只跑「離線測試」（不連網、不下單、安全快速）：
    python run_tests.py

加上 --live 會額外跑「testnet 真實下單鏈路」測試（需要 .env 已設好 testnet 金鑰）：
    python run_tests.py --live
"""

import subprocess
import sys

OFFLINE_TESTS = [
    ("解析/格式化/標記#", "test_parsing.py", []),
    ("對帳/移保本/收單", "test_reconcile.py", []),
]
LIVE_TESTS = [
    ("testnet 下單鏈路", "test_trade.py", ["--reset"]),
]


def run(label: str, script: str, args: list) -> bool:
    print(f"\n{'=' * 56}\n▶ {label}  ({script} {' '.join(args)})\n{'=' * 56}", flush=True)
    return subprocess.run([sys.executable, script, *args]).returncode == 0


def main() -> None:
    live = "--live" in sys.argv
    suite = list(OFFLINE_TESTS)
    if live:
        suite += LIVE_TESTS

    results = [(label, run(label, script, args)) for label, script, args in suite]

    print(f"\n{'=' * 56}\n  測試總結\n{'=' * 56}")
    for label, ok in results:
        print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {label}")
    if not live:
        print("\n（未含 testnet 實單測試；要一起跑請加 --live）")

    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
