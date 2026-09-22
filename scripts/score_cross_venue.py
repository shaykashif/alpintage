"""Checks, once each logged game settles, whether Kalshi's price or the
cross-venue (sportsbook consensus / Polymarket) price was closer to the real
outcome. This is the evidence step before any paper trade is allowed to act
on a cross-venue divergence -- the same discipline fair_value.py applies to
Jev: a statistical-arbitrage hypothesis ("Kalshi converges toward the
sportsbook consensus") needs to survive contact with real settlements
before anything sizes a bet on it, exactly like a true arbitrage doesn't
need to (it's a mathematical guarantee, not a hypothesis).

Usage:
    uv run python scripts/score_cross_venue.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from kalshi_engine.kalshi_public import PublicClient  # noqa: E402
from run_cross_venue_scanner import best_team_assignment  # noqa: E402

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "cross_venue.jsonl"
SCORED_PATH = Path(__file__).resolve().parent.parent / "data" / "cross_venue_scored.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def main() -> None:
    rows = _load_jsonl(LOG_PATH)
    if not rows:
        print(f"nothing logged yet at {LOG_PATH}")
        return

    already_scored = {r["kalshi_event_ticker"] for r in _load_jsonl(SCORED_PATH)}
    client = PublicClient()
    newly_scored = []

    for row in rows:
        ticker = row["kalshi_event_ticker"]
        if ticker in already_scored:
            continue

        try:
            markets = client.markets(event_ticker=ticker, status="settled")
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: could not fetch ({exc})")
            continue
        if len(markets) < 2:
            continue  # not settled yet (or an odd event shape) -- try again later

        outcomes: dict[str, int] = {}
        for m in markets:
            result = m.get("result")
            if result not in ("yes", "no"):
                outcomes = {}
                break
            team = m.get("yes_sub_title") or m["ticker"]
            outcomes[team] = 1 if result == "yes" else 0
        if len(outcomes) != 2:
            continue

        kalshi_probs = row["kalshi_probs"]
        kalshi_pairs = [(kalshi_probs[t], outcomes[t]) for t in kalshi_probs if t in outcomes]

        scored = dict(row)
        scored["outcomes"] = outcomes
        scored["kalshi_brier"] = _brier(kalshi_pairs)

        for venue_key, probs_key in (("odds_api", "odds_api_avg_probs"), ("polymarket", "polymarket_probs")):
            venue_probs = row.get(probs_key)
            if not venue_probs:
                scored[f"{venue_key}_brier"] = None
                continue
            pairs_for_venue = best_team_assignment(kalshi_probs, venue_probs)
            venue_pairs = [(venue_probs[o], outcomes[k]) for k, o in pairs_for_venue if k in outcomes]
            scored[f"{venue_key}_brier"] = _brier(venue_pairs)

        newly_scored.append(scored)
        print(
            f"  {ticker}: outcomes={outcomes} kalshi_brier={scored['kalshi_brier']:.4f} "
            f"odds_api_brier={scored['odds_api_brier']} polymarket_brier={scored['polymarket_brier']}"
        )

    if newly_scored:
        with SCORED_PATH.open("a", encoding="utf-8") as f:
            for row in newly_scored:
                f.write(json.dumps(row) + "\n")
        print(f"\nscored {len(newly_scored)} newly-settled games, appended to {SCORED_PATH}")

    all_scored = _load_jsonl(SCORED_PATH)
    print(f"\n{len(all_scored)} games scored total")
    if len(all_scored) < 20:
        print("not enough settled games yet for a meaningful comparison (want 20+, ideally 100+)")
        return

    kalshi_briers = [r["kalshi_brier"] for r in all_scored if r["kalshi_brier"] is not None]
    odds_briers = [r["odds_api_brier"] for r in all_scored if r.get("odds_api_brier") is not None]
    poly_briers = [r["polymarket_brier"] for r in all_scored if r.get("polymarket_brier") is not None]

    print(f"\nKalshi Brier (n={len(kalshi_briers)}):     {sum(kalshi_briers)/len(kalshi_briers):.4f}")
    if odds_briers:
        print(f"Odds API Brier (n={len(odds_briers)}):   {sum(odds_briers)/len(odds_briers):.4f}")
    if poly_briers:
        print(f"Polymarket Brier (n={len(poly_briers)}): {sum(poly_briers)/len(poly_briers):.4f}")
    print("\nLower is better. If a venue's Brier beats Kalshi's here, that's the first real")
    print("evidence a cross-venue divergence predicts anything -- see cross_venue_trust.py.")


if __name__ == "__main__":
    main()
