"""Run the scanner, the Jev collector, and (periodically) the cross-venue
comparison + scoring on a repeating interval, for multi-day unattended
paper testing. Nothing here sends a real order.

run_scanner.py --paper trades on true arbitrage (ladder/bracket violations
-- a mathematical guarantee). run_cross_venue_scanner.py --paper trades on
statistical arbitrage (Kalshi vs. sportsbook/Polymarket divergence -- a
hypothesis, not a guarantee) ONLY once score_cross_venue.py has logged
enough settled games to show that hypothesis actually holds; see
cross_venue_trust.py. Until then it only logs, same as before.

Usage:
    uv run python scripts/run_loop.py
    uv run python scripts/run_loop.py --interval-min 30 --collect-n 10
    uv run python scripts/run_loop.py --cycles 3   # test it, then stop
    uv run python scripts/run_loop.py --cross-venue-every 24 --cross-venue-leagues nfl,nba

Stop with Ctrl+C. Safe to interrupt and restart: collect_predictions.py
skips tickers it already logged, run_scanner.py skips paper-buying a ticker
it already holds a logged fill on, and the cross-venue cycle count just
restarts from 0 (it re-runs sooner than scheduled after a restart, which is
harmless -- it never re-logs a candidate whose game already settled, but it
also doesn't dedupe by ticker the way the other two scripts do, so a
restart-heavy deployment will produce some duplicate rows in
cross_venue.jsonl. Not a correctness problem for the dashboard, which reads
all rows each time, just a minor quota cost to know about).

Cross-venue scanning costs real Odds API quota (~500/month free tier) and
takes minutes, not seconds (a 5-league scan can take 2-3 minutes), so it
runs far less often than the other two -- every --cross-venue-every cycles,
not every cycle. Default (48 cycles at the default 30-min interval = once
a day) keeps a 5-league scan comfortably under quota; tune both flags
together if you change the interval.
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


def _run(cmd: list[str], timeout: int = 120) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    header = f"\n[{ts}] $ {' '.join(cmd)}"
    print(header)
    _append_log(header)
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
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
    ap.add_argument("--cross-venue-every", type=int, default=48, help="run the cross-venue scan every N cycles (0 = never)")
    ap.add_argument("--cross-venue-leagues", default="all", help="comma-separated league keys, or 'all'")
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

            if args.cross_venue_every and (cycle == 1 or cycle % args.cross_venue_every == 0):
                # --paper: acts on a cross-venue divergence ONLY if
                # cross_venue_trust.py says that venue has earned it (30+
                # settled games where it beat Kalshi's own Brier score).
                # Until score_cross_venue.py below has logged that much
                # evidence, this flag is a no-op -- see run_cross_venue_scanner.py.
                _run(
                    [python, "scripts/run_cross_venue_scanner.py", "--leagues", args.cross_venue_leagues, "--paper"],
                    timeout=600,  # a full multi-league scan can take minutes, not seconds
                )
                _run([python, "scripts/score_cross_venue.py"], timeout=180)

            if args.cycles and cycle >= args.cycles:
                print("reached --cycles limit, stopping")
                break
            print(f"sleeping {args.interval_min} min...")
            time.sleep(args.interval_min * 60)
    except KeyboardInterrupt:
        print("\nstopped by user")


if __name__ == "__main__":
    main()
