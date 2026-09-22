"""Run the scanner and the Jev collector on a repeating interval, for
multi-day unattended paper testing. Nothing here sends a real order --
run_scanner.py --paper is simulated, collect_predictions.py only logs.

Usage:
    uv run python scripts/run_loop.py
    uv run python scripts/run_loop.py --interval-min 30 --collect-n 10
    uv run python scripts/run_loop.py --cycles 3   # test it, then stop

Stop with Ctrl+C. Safe to interrupt and restart: collect_predictions.py
skips tickers it already logged, and run_scanner.py (as of this version)
skips paper-buying a ticker it already holds a logged fill on.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "loop.log"


def _run(cmd: list[str]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    header = f"\n[{ts}] $ {' '.join(cmd)}"
    print(header)
    _append_log(header)
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
        out = result.stdout + result.stderr
    except Exception as exc:  # noqa: BLE001
        out = f"FAILED TO RUN: {exc}"
    print(out)
    _append_log(out)


def _append_log(text: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-min", type=float, default=30.0, help="minutes between cycles")
    ap.add_argument("--scan-pages", type=int, default=10)
    ap.add_argument("--collect-n", type=int, default=10)
    ap.add_argument("--series-ticker", default=None, help="restrict Jev collection to one series")
    ap.add_argument("--cycles", type=int, default=0, help="stop after N cycles, 0 = run forever")
    args = ap.parse_args()

    python = sys.executable
    cycle = 0
    print(f"starting loop: every {args.interval_min} min, logging to {LOG_PATH}. Ctrl+C to stop.")
    try:
        while True:
            cycle += 1
            print(f"\n=== cycle {cycle} ===")
            _run([python, "scripts/run_scanner.py", "--max-pages", str(args.scan_pages), "--paper"])

            collect_cmd = [python, "scripts/collect_predictions.py", "--n", str(args.collect_n)]
            if args.series_ticker:
                collect_cmd += ["--series-ticker", args.series_ticker]
            _run(collect_cmd)

            _run([python, "scripts/score_predictions.py"])
            _run([python, "scripts/score_paper_fills.py"])

            if args.cycles and cycle >= args.cycles:
                print("reached --cycles limit, stopping")
                break
            print(f"sleeping {args.interval_min} min...")
            time.sleep(args.interval_min * 60)
    except KeyboardInterrupt:
        print("\nstopped by user")


if __name__ == "__main__":
    main()
