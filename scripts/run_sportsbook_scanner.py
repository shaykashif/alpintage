"""Scan live sportsbook odds for surebets (best price per outcome across
books beats 100% implied probability). Detection only -- never places a bet
at any sportsbook. The Odds API's free tier is quota-limited (check the
"quota remaining" line after every run), so this defaults to one sport.

Usage:
    uv run python scripts/run_sportsbook_scanner.py
    uv run python scripts/run_sportsbook_scanner.py --sport nba
    uv run python scripts/run_sportsbook_scanner.py --sport nfl --stake 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from kalshi_engine.devig import american_to_decimal  # noqa: E402
from kalshi_engine.odds_client import SPORT_KEYS, get_odds  # noqa: E402
from kalshi_engine.surebet import OddsQuote, find_surebet  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="nfl", choices=sorted(SPORT_KEYS), help="which sport to scan")
    ap.add_argument("--stake", type=float, default=100.0, help="total stake to size each surebet at")
    ap.add_argument("--regions", default="us")
    args = ap.parse_args()

    load_dotenv()
    events, quota = get_odds(SPORT_KEYS[args.sport], regions=args.regions, markets="h2h")
    print(f"scanned {len(events)} {args.sport.upper()} events (quota remaining: {quota.requests_remaining})\n")

    hits = 0
    for e in events:
        quotes = [
            OddsQuote(bookmaker=bm["key"], outcome=outcome["name"], american_odds=outcome["price"])
            for bm in e.get("bookmakers", [])
            for market in bm.get("markets", [])
            if market["key"] == "h2h"
            for outcome in market["outcomes"]
        ]
        if not quotes:
            continue

        result = find_surebet(quotes, total_stake_usd=args.stake)
        label = f"{e.get('away_team', '?')} @ {e.get('home_team', '?')}"

        if result:
            hits += 1
            print(f"{label}: SUREBET, profit ${result.profit_usd} ({result.profit_pct:.2%})")
            for leg in result.legs:
                print(f"    bet ${leg.stake_usd} on {leg.outcome} @ {leg.bookmaker} ({leg.american_odds:+.0f})")
        else:
            best_by_outcome: dict[str, float] = {}
            for q in quotes:
                d = american_to_decimal(q.american_odds)
                if q.outcome not in best_by_outcome or d > best_by_outcome[q.outcome]:
                    best_by_outcome[q.outcome] = d
            total_inv = sum(1 / d for d in best_by_outcome.values())
            print(f"{label}: no surebet (best-price implied total {total_inv:.4f})")

    print(f"\n{hits} surebet(s) found across {len(events)} events")
    if hits == 0:
        print("expected result most of the time on mainstream games at major books")


if __name__ == "__main__":
    main()
