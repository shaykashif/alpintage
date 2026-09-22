"""Odds math shared by both sportsbook-arb detectors: book-vs-book surebets
and Kalshi-vs-sportsbook cross-venue comparison.

A sportsbook's quoted odds always imply probabilities that sum to more than
100% -- that excess is the vig (the book's built-in edge). To compare a book's
price to a "fair" probability (or to Kalshi's price), the vig has to be
removed first. This uses the standard multiplicative method: scale every
outcome's implied probability down proportionally so they sum to exactly 1.
It's the simplest de-vig method, not the only one (power and Shin methods
exist and can matter more on lopsided lines) -- fine for a first pass.
"""
from __future__ import annotations

from dataclasses import dataclass


def american_to_prob(odds: float) -> float:
    """Convert American odds (e.g. -110, +145) to raw implied probability
    (still includes the vig)."""
    if odds > 0:
        return 100 / (odds + 100)
    if odds < 0:
        return -odds / (-odds + 100)
    raise ValueError("American odds cannot be 0")


def american_to_decimal(odds: float) -> float:
    """Convert American odds to decimal odds (e.g. -110 -> 1.909, +150 -> 2.5)."""
    if odds > 0:
        return 1 + odds / 100
    if odds < 0:
        return 1 + 100 / -odds
    raise ValueError("American odds cannot be 0")


def decimal_to_prob(odds: float) -> float:
    """Convert decimal odds (e.g. 1.91, 2.45) to raw implied probability."""
    if odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    return 1 / odds


def devig_multiplicative(raw_probs: list[float]) -> list[float]:
    """Scale a list of raw (vig-included) implied probabilities so they sum
    to exactly 1.0. Works for 2-way or n-way markets."""
    total = sum(raw_probs)
    if total <= 0:
        raise ValueError("probabilities must sum to > 0")
    return [p / total for p in raw_probs]


@dataclass
class BookLine:
    bookmaker: str
    outcome: str
    american_odds: float


def fair_probs(lines: list[BookLine]) -> dict[str, float]:
    """Given every outcome's price from ONE bookmaker for one market (e.g.
    both sides of a moneyline), return the de-vigged fair probability per
    outcome. All `lines` must be from the same bookmaker and the same
    market -- call this once per bookmaker, not across books."""
    raw = [american_to_prob(l.american_odds) for l in lines]
    devigged = devig_multiplicative(raw)
    return {l.outcome: p for l, p in zip(lines, devigged)}


def vig_pct(raw_probs: list[float]) -> float:
    """The bookmaker's overround, e.g. 0.045 = 4.5% vig."""
    return sum(raw_probs) - 1.0


def aggregate_fair_probs(h2h_bookmakers: list[dict]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Given The Odds API's raw `bookmakers` list for one event, de-vig every
    book's h2h line individually, then average across books per outcome.
    Using more than one book (instead of just the first) catches an outlier
    book that's mispriced relative to the consensus, not just relative to
    Kalshi. Returns (average fair prob per outcome, per-book fair probs)."""
    per_book: dict[str, dict[str, float]] = {}
    for bm in h2h_bookmakers:
        h2h = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
        if not h2h or len(h2h.get("outcomes", [])) < 2:
            continue
        lines = [BookLine(bookmaker=bm["key"], outcome=o["name"], american_odds=o["price"]) for o in h2h["outcomes"]]
        try:
            per_book[bm["key"]] = fair_probs(lines)
        except (ValueError, ZeroDivisionError):
            continue

    if not per_book:
        return {}, {}

    outcomes = {o for probs in per_book.values() for o in probs}
    avg = {o: sum(probs.get(o, 0.0) for probs in per_book.values()) / len(per_book) for o in outcomes}
    return avg, per_book
