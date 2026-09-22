"""Kalshi fee math. The 0.07 taker coefficient and the "maker fee is usually
lower or zero" assumption are from memory -- verify both against Kalshi's
current fee schedule before trusting any edge calculation built on this.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

TAKER_COEFF = Decimal("0.07")
CENT = Decimal("0.01")


def taker_fee(contracts: int | float, price: Decimal | float | str) -> Decimal:
    """Fee in dollars for taking `contracts` at `price` (dollars, 0 to 1),
    rounded up to the cent. Applies per leg -- a multi-leg trade needs one
    call per leg, summed."""
    p = Decimal(str(price))
    c = Decimal(str(contracts))
    raw = TAKER_COEFF * c * p * (1 - p)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def total_taker_fee(legs: list[tuple[int | float, Decimal | float | str]]) -> Decimal:
    """Sum of taker_fee() across several (contracts, price) legs, e.g. every
    leg of a bracket-sum or ladder trade."""
    return sum((taker_fee(c, p) for c, p in legs), Decimal("0.00"))
