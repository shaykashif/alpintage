"""Run two paper-trading models on a repeating interval, for multi-day
unattended operation. Nothing here sends a real order.

1. run_scanner.py --paper: true arbitrage within Kalshi (ladder/bracket
   violations -- a mathematical guarantee). Runs every cycle.
2. run_cross_venue_scanner.py --paper: Kalshi vs. sportsbook (The Odds API)
   / Polymarket mispricing, with Jev used ONLY to confirm two listings
   describe the same real-world game (never to set a price or a trade
   decision). POC mode by default: trades on any sufficiently large
   divergence net of fees, no statistical validation gate -- see that
   script's docstring for the honest caveat on what that does and doesn't
   prove. Runs periodically (--cross-venue-every), not every cycle, since
   it costs real Odds API quota.

This project's earlier, separate "ask Jev to predict any Kalshi market"
research loop (collect_predictions.py / score_predictions.py) has been
removed from this automated pipeline per the project owner's choice -- Jev
now only does event-matching here, nothing else. The scripts still exist
and still work if you want to run that research manually.

Usage:
    uv run python scripts/run_loop.py
    uv run python scripts/run_loop.py --interval-min 30
    uv run python scripts/run_loop.py --cycles 3   # test it, then stop
    uv run python scripts/run_loop.py --cross-venue-every 6 --cross-venue-leagues nfl,mlb

Stop with Ctrl+C. Safe to interrupt and restart: run_scanner.py and
run_cross_venue_scanner.py both skip re-buying a ticker they already hold
a logged fill on. The cross-venue cycle count restarts from 0 on restart
(re-runs sooner than scheduled -- harmless, just a minor extra quota cost).

Cross-venue scanning takes minutes, not seconds (a multi-league scan can
take 2-3 minutes) and costs real Odds API quota (~500/month free tier), so
it runs far less often than the ladder/bracket check. Defaults (every 6
cycles at the default 30-min interval = every 3 hours, NFL only) keep it
comfortably under quota while still giving it several chances a day to
find and act on something; widen --cross-venue-leagues if you want more
coverage and are fine spending more quota.
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
    ap.add_argument("--cycles", type=int, default=0, help="stop after N cycles, 0 = run forever")
    ap.add_argument("--cross-venue-every", type=int, default=6, help="run the cross-venue model every N cycles (0 = never)")
    ap.add_argument("--cross-venue-leagues", default="nfl", help="comma-separated league keys, or 'all' (costs more Odds API quota)")
    ap.add_argument("--require-trust", action="store_true", help="pass through to run_cross_venue_scanner.py's stricter, evidence-gated mode")
    args = ap.parse_args()

    python = sys.executable
    cycle = 0
    print(f"starting loop: every {args.interval_min} min, logging to {LOG_PATH}. Ctrl+C to stop.")
    try:
        while True:
            cycle += 1
            print(f"\n=== cycle {cycle} ===")
            _run([python, "scripts/run_scanner.py", "--max-pages", str(args.scan_pages), "--paper"])
            _run([python, "scripts/score_paper_fills.py"])

            if args.cross_venue_every and (cycle == 1 or cycle % args.cross_venue_every == 0):
                cv_cmd = [python, "scripts/run_cross_venue_scanner.py", "--leagues", args.cross_venue_leagues, "--paper"]
                if args.require_trust:
                    cv_cmd.append("--require-trust")
                _run(cv_cmd, timeout=600)  # a multi-league scan can take minutes, not seconds
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
