"""Combines the market's own price with a model signal (Jev) into one fair
value estimate -- but ONLY lets the model move the number once its logged
predictions have actually beaten the market on real settled outcomes. Until
then this is a no-op that just returns the market price, on purpose.

This is the enforcement of the rule from the project conversation: a
probabilistic signal is a feature with unknown alpha until proven otherwise,
never a number you trust by default.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "predictions.jsonl"

MIN_SCORED_FOR_TRUST = 30  # below this, treat the signal as unproven regardless of Brier score
MAX_BLEND_WEIGHT = 0.5  # even a validated signal never dominates the market price outright


def _scored_rows() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    rows = []
    with DATA_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("outcome") is not None:
                rows.append(row)
    return rows


def _brier(rows: list[dict], prob_key: str) -> float:
    return sum((r[prob_key] - r["outcome"]) ** 2 for r in rows) / len(rows)


def jev_trust_weight(min_scored: int = MIN_SCORED_FOR_TRUST) -> tuple[float, str]:
    """Returns (weight, reason). weight is 0.0 unless data/predictions.jsonl
    has at least `min_scored` settled rows AND Jev's Brier score beats the
    market's on them. Recompute this each time you use it -- it changes as
    score_predictions.py logs more settled outcomes."""
    rows = _scored_rows()
    if len(rows) < min_scored:
        return 0.0, f"only {len(rows)} scored predictions logged, need {min_scored}+ to trust the signal"

    jev_brier = _brier(rows, "jev_prob")
    market_brier = _brier(rows, "market_prob")
    if jev_brier >= market_brier:
        return 0.0, f"Jev Brier {jev_brier:.4f} does not beat market Brier {market_brier:.4f} on {len(rows)} rows"

    # Scale the weight by how much better Jev is, capped at MAX_BLEND_WEIGHT.
    improvement = (market_brier - jev_brier) / market_brier
    weight = min(MAX_BLEND_WEIGHT, improvement)
    return weight, f"Jev beats market ({jev_brier:.4f} < {market_brier:.4f}) on {len(rows)} rows, weight={weight:.3f}"


def blended_fair_value(market_prob: float, jev_prob: float | None = None) -> tuple[float, str]:
    """Fair value estimate. Returns (probability, explanation). If jev_prob
    is None, or the signal hasn't earned trust yet, this just returns
    market_prob unchanged."""
    if jev_prob is None:
        return market_prob, "no Jev signal supplied"

    weight, reason = jev_trust_weight()
    if weight == 0.0:
        return market_prob, f"Jev signal ignored: {reason}"

    blended = (1 - weight) * market_prob + weight * jev_prob
    return blended, f"blended at weight {weight:.3f}: {reason}"
