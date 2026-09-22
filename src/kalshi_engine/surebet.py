"""Book-vs-book surebet detection: for one event's market (e.g. moneyline),
find the best price on each outcome across different sportsbooks. If the
best odds' implied probabilities (raw, vig included) sum to less than 1,
betting the right amount on each outcome guarantees a profit regardless of
which one wins -- that's the classic sportsbook arb.

No de-vig here on purpose: a surebet is about what you can ACTUALLY get
(the best real quoted price), not a fair-value comparison. devig.py's
de-vig math is for the Kalshi-vs-sportsbook comparison instead, where you
need each book's fair (vig-removed) probability to compare against Kalshi's
price.

This only detects and sizes a candidate -- it does not place any bet at any
book. Doing that needs an account and credentials at each book, which is
explicitly out of scope here; see the project conversation history for why.
"""
from __future__ import annotations

from dataclasses import dataclass

from .devig import american_to_decimal, american_to_prob


@dataclass
class OddsQuote:
    bookmaker: str
    outcome: str
    american_odds: float


@dataclass
class SurebetLeg:
    outcome: str
    bookmaker: str
    american_odds: float
    decimal_odds: float
    stake_usd: float
    payout_if_wins_usd: float


@dataclass
class SurebetResult:
    legs: list[SurebetLeg]
    total_stake_usd: float
    guaranteed_payout_usd: float  # same regardless of which leg wins, by construction
    profit_usd: float
    profit_pct: float  # profit / total_stake


def best_price_per_outcome(quotes: list[OddsQuote]) -> dict[str, OddsQuote]:
    """For each outcome, the single best (highest decimal-equivalent) quote
    across all bookmakers passed in."""
    best: dict[str, OddsQuote] = {}
    for q in quotes:
        current = best.get(q.outcome)
        if current is None or american_to_decimal(q.american_odds) > american_to_decimal(current.american_odds):
            best[q.outcome] = q
    return best


def find_surebet(quotes: list[OddsQuote], total_stake_usd: float = 100.0) -> SurebetResult | None:
    """quotes should cover every outcome of ONE market for ONE event, from
    however many bookmakers you have prices for. Returns None if there's no
    guaranteed-profit combination at the best available prices."""
    best = best_price_per_outcome(quotes)
    if len(best) < 2:
        return None

    inv_decimal = {o: 1 / american_to_decimal(q.american_odds) for o, q in best.items()}
    total_inv = sum(inv_decimal.values())
    if total_inv >= 1.0:
        return None  # no edge -- this is the expected, normal result most of the time

    legs = []
    for outcome, q in best.items():
        stake = total_stake_usd * inv_decimal[outcome] / total_inv
        decimal_odds = american_to_decimal(q.american_odds)
        legs.append(SurebetLeg(
            outcome=outcome,
            bookmaker=q.bookmaker,
            american_odds=q.american_odds,
            decimal_odds=decimal_odds,
            stake_usd=round(stake, 2),
            payout_if_wins_usd=round(stake * decimal_odds, 2),
        ))

    guaranteed_payout = legs[0].payout_if_wins_usd  # equal by construction across legs
    profit = guaranteed_payout - total_stake_usd
    return SurebetResult(
        legs=legs,
        total_stake_usd=total_stake_usd,
        guaranteed_payout_usd=round(guaranteed_payout, 2),
        profit_usd=round(profit, 2),
        profit_pct=round(profit / total_stake_usd, 4),
    )
