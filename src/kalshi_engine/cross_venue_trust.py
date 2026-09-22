"""Whether a cross-venue signal (sportsbook consensus, Polymarket) has
earned the right to size a paper trade. Same discipline as
fair_value.jev_trust_weight() applies to Jev: statistical arbitrage is a
hypothesis about convergence, not a guarantee like the ladder/bracket
checks are, so nothing trades on it until it's demonstrably beaten Kalshi's
own price on real settled games.
"""
from __future__ import annotations

import json
from pathlib import Path

SCORED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cross_venue_scored.jsonl"
MIN_SCORED_FOR_TRUST = 30


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


def venue_trust(venue_key: str, min_scored: int = MIN_SCORED_FOR_TRUST) -> tuple[bool, str]:
    """venue_key: 'odds_api' or 'polymarket'. Returns (trusted, reason).
    Recompute this every time it's used -- it changes as
    score_cross_venue.py logs more settled games."""
    rows = [
        r for r in _load_jsonl(SCORED_PATH)
        if r.get(f"{venue_key}_brier") is not None and r.get("kalshi_brier") is not None
    ]
    if len(rows) < min_scored:
        return False, f"only {len(rows)} scored games with {venue_key} data, need {min_scored}+ to trust it"

    venue_brier = sum(r[f"{venue_key}_brier"] for r in rows) / len(rows)
    kalshi_brier = sum(r["kalshi_brier"] for r in rows) / len(rows)
    if venue_brier >= kalshi_brier:
        return False, f"{venue_key} Brier {venue_brier:.4f} does not beat Kalshi's {kalshi_brier:.4f} on {len(rows)} games"
    return True, f"{venue_key} beats Kalshi ({venue_brier:.4f} < {kalshi_brier:.4f}) on {len(rows)} games"
