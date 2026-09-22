"""Scan live open Kalshi markets for ladder and bracket-sum violations, net of
fees. Prints every candidate found. With --paper, also sends each leg through
the risk-gated PaperBroker -- simulated fills only, nothing touches a real
Kalshi order.

Usage:
    uv run python scripts/run_scanner.py
    uv run python scripts/run_scanner.py --max-pages 30
    uv run python scripts/run_scanner.py --paper
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine.kalshi_public import PublicClient  # noqa: E402
from kalshi_engine.paper_broker import DEFAULT_LOG_PATH, PaperBroker  # noqa: E402
from kalshi_engine.scanner import find_bracket_sum_violations, find_ladder_violations  # noqa: E402


def _already_filled_tickers(log_path: Path = DEFAULT_LOG_PATH) -> set[str]:
    """Tickers with a real fill already logged, across every past run of this
    script -- so a multi-cycle loop (run_loop.py) doesn't re-buy the same
    recurring violation every cycle. The PaperBroker itself starts fresh
    (in-memory) on every script invocation, so this file is the only thing
    that makes repeated runs aware of what's already been taken."""
    if not log_path.exists():
        return set()
    tickers = set()
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "fill":
                tickers.add(row["ticker"])
    return tickers


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=15)
    ap.add_argument("--paper", action="store_true", help="also paper-trade any violation found")
    args = ap.parse_args()

    client = PublicClient()
    markets = client.markets(max_pages=args.max_pages, status="open", mve_filter="exclude")
    print(f"scanned {len(markets)} open markets")

    ladder_hits = find_ladder_violations(markets)
    bracket_hits = find_bracket_sum_violations(markets)

    print(f"\nladder violations: {len(ladder_hits)}")
    for v in ladder_hits:
        print(
            f"  {v.event_ticker}: buy YES {v.low_ticker} @{v.low_yes_ask:.2f}, "
            f"buy NO {v.high_ticker} (bid {v.high_yes_bid:.2f}), "
            f"fees ${v.fees_usd:.2f}, edge ${v.edge_usd:.4f}/contract"
        )

    print(f"\nbracket-sum violations: {len(bracket_hits)}")
    for v in bracket_hits:
        print(
            f"  {v.event_ticker}: buy YES on all {len(v.tickers)} legs, "
            f"sum(ask)=${v.sum_yes_ask:.2f}, fees ${v.fees_usd:.2f}, "
            f"edge ${v.edge_usd:.4f}/contract-set"
        )

    if not ladder_hits and not bracket_hits:
        print("\nno violations found net of fees -- this is the expected, normal result")

    if not args.paper:
        return

    print("\n--- paper trading (simulated fills, no real orders) ---")
    already_filled = _already_filled_tickers()
    broker = PaperBroker()
    for v in ladder_hits:
        if v.low_ticker in already_filled or v.high_ticker in already_filled:
            print(f"  skipping {v.event_ticker}: already have a paper fill on one of these legs")
            continue
        broker.buy(v.low_ticker, "yes", v.low_yes_ask, qty=1, reason="ladder violation")
        no_ask_high = round(1 - v.high_yes_bid, 4)
        broker.buy(v.high_ticker, "no", no_ask_high, qty=1, reason="ladder violation")
    for v in bracket_hits:
        if any(t in already_filled for t in v.tickers):
            print(f"  skipping {v.event_ticker}: already have a paper fill on one of these legs")
            continue
        # re-fetch each leg's ask isn't necessary here -- edge_usd was computed
        # from sum_yes_ask, but we need the individual leg asks to place each
        # order. Recompute from the tickers by looking them back up.
        by_ticker = {m["ticker"]: m for m in markets}
        for t in v.tickers:
            m = by_ticker[t]
            broker.buy(t, "yes", float(m["yes_ask_dollars"]), qty=1, reason="bracket-sum violation")

    print(f"paper cash remaining: ${broker.cash_usd:.2f}")
    print(f"open paper positions: {len(broker.positions)}")
    print(f"fills/vetoes logged to {broker.log_path}")


if __name__ == "__main__":
    main()
