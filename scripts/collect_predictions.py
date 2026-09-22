"""Log Jev's judgment on text/event-shaped Kalshi markets, next to the market's
own implied probability, WITHOUT trading anything. Run this periodically (a
few times a day) to build a sample. Nothing here places an order.

Usage:
    uv run python scripts/collect_predictions.py --n 20
    uv run python scripts/collect_predictions.py --n 20 --series-ticker KXNFLRACE

Output: appends JSONL rows to data/predictions.jsonl (gitignored).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from kalshi_engine.jev_client import ask_noul  # noqa: E402
from kalshi_engine.kalshi_public import (  # noqa: E402
    PublicClient,
    build_state_text,
    is_text_shaped,
    market_implied_prob,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.jsonl"

NOUL_INSTRUCTIONS = (
    "Based only on the information given, will the described condition happen "
    "(i.e. will this resolve YES)? Answer from general knowledge and reasoning "
    "about the situation, not from any market price."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="max markets to log this run")
    ap.add_argument("--series-ticker", default=None, help="restrict to one series, e.g. KXNFLRACE")
    ap.add_argument("--max-pages", type=int, default=15, help="pages of /markets to scan for candidates")
    args = ap.parse_args()

    load_dotenv()
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    already_logged = _already_logged_tickers()

    client = PublicClient()
    params = {"status": "open", "mve_filter": "exclude"}
    if args.series_ticker:
        params["series_ticker"] = args.series_ticker

    markets = client.markets(max_pages=args.max_pages, **params)
    print(f"scanned {len(markets)} open markets")

    candidates = [
        m for m in markets
        if is_text_shaped(m)
        and m["ticker"] not in already_logged
        and market_implied_prob(m) is not None
    ]
    print(f"{len(candidates)} text-shaped candidates with a real quote and not yet logged")

    rows_written = 0
    with DATA_PATH.open("a", encoding="utf-8") as f:
        for m in candidates[: args.n]:
            state = build_state_text(m)
            if not state:
                continue
            market_prob = market_implied_prob(m)
            try:
                result = ask_noul(state, NOUL_INSTRUCTIONS)
            except Exception as exc:  # noqa: BLE001
                print(f"  {m['ticker']}: Jev call failed ({exc}), skipping")
                continue

            row = {
                "logged_at": datetime.now(timezone.utc).isoformat(),
                "ticker": m["ticker"],
                "event_ticker": m["event_ticker"],
                "close_time": m.get("close_time"),
                "market_prob": market_prob,
                "jev_prob": result.prob,
                "jev_route": result.route,
                "jev_model": result.model,
                "jev_latency_ms": round(result.latency_ms, 1),
                "state_text": state,
                "outcome": None,  # filled in later by score_predictions.py
                "scored_at": None,
            }
            f.write(json.dumps(row) + "\n")
            rows_written += 1
            print(
                f"  {m['ticker']}: market={market_prob:.3f} jev={result.prob:.3f} "
                f"({result.route}, {result.latency_ms:.0f}ms)"
            )
            time.sleep(0.2)  # be polite to both APIs

    print(f"logged {rows_written} new rows to {DATA_PATH}")
    if rows_written == 0:
        print("nothing new logged this run")
    elif not _has_real_route():
        print(
            "WARNING: all rows used the mock Jev client (no TYPESAFE_API_KEY set). "
            "This proves the plumbing works, not whether Jev is useful. Set "
            "TYPESAFE_API_KEY in .env and re-run to log real predictions."
        )


def _already_logged_tickers() -> set[str]:
    if not DATA_PATH.exists():
        return set()
    tickers = set()
    with DATA_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                tickers.add(json.loads(line)["ticker"])
            except (json.JSONDecodeError, KeyError):
                continue
    return tickers


def _has_real_route() -> bool:
    if not DATA_PATH.exists():
        return False
    with DATA_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                if json.loads(line).get("jev_route") == "typesafe":
                    return True
            except json.JSONDecodeError:
                continue
    return False


if __name__ == "__main__":
    main()
