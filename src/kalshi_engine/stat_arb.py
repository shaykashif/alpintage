"""Cross-venue statistical arbitrage: the pure decision logic, no I/O.

The thesis: a de-vigged sportsbook consensus (or Polymarket) is a better
estimate of a game's true probability than Kalshi's quote, so when Kalshi's
YES ask sits below that reference by more than fees, buy it and expect
Kalshi to converge toward the reference. That is a HYPOTHESIS, not a
guarantee -- unlike the ladder/bracket arbs in scanner.py, nothing forces
convergence, and score_cross_venue.py / cross_venue_trust.py exist to test
whether it's true.

Lifecycle of one position:
  1. Entry  -- net-of-fee edge (fair - ask - fee/contract) clears
               MIN_ENTRY_EDGE and stays under MAX_PLAUSIBLE_EDGE.
  2. Sizing -- fractional Kelly on the paper bankroll, capped by the risk
               gate's per-order notional. Binary contract bought at `ask`
               with win probability `fair`: f* = (fair - ask) / (1 - ask).
  3. Exit   -- re-checked every scan with FRESH prices from both venues:
               * converged: Kalshi's bid (net of the exit fee) has reached
                 the reference fair value -- the spread closed, take it.
               * reference broken: the spread blew out past
                 MAX_PLAUSIBLE_EDGE -- far likelier a stale/mismatched
                 reference (or the game going live) than more edge.
               Otherwise hold; settlement is the fallback exit, and at
               settlement a correctly-priced reference still has positive
               expected value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .fees import taker_fee

# POC-appropriate entry bar (observed real divergences mostly sit at 1-3%).
# Not a validated edge threshold -- lower means more trades.
MIN_ENTRY_EDGE = 0.005

# An edge bigger than this is far more likely a matching bug than real
# mispricing -- caught live, a same-team-different-day series game got
# mis-paired and produced a fake 44.5-point "edge". Never auto-traded.
MAX_PLAUSIBLE_EDGE = 0.20

# Exit once the remaining edge on the bid side is at or below this.
# 0.0 = sell as soon as Kalshi's bid, net of fee, reaches the reference.
EXIT_EDGE = 0.0

# Only trade quotes inside this band: de-vigged probabilities are least
# reliable at the extremes (favorite-longshot bias), and a 1-cent tick is a
# huge relative error on a 3-cent contract.
PRICE_BAND = (0.05, 0.95)

KELLY_FRACTION = 0.25  # quarter-Kelly: full Kelly on an unvalidated edge is reckless


@dataclass
class EntrySignal:
    qty: int
    edge: float  # per contract, net of the entry fee at this qty
    reason: str | None = None  # why NOT to trade, when qty == 0


def fee_per_contract(qty: int, price: float) -> float:
    return float(taker_fee(qty, price)) / qty


def kelly_qty(fair: float, ask: float, bankroll_usd: float, max_notional_usd: float,
              kelly_fraction: float = KELLY_FRACTION) -> int:
    """Contracts to buy: fractional Kelly stake, capped by max_notional_usd,
    at least 1 if there's any positive Kelly fraction at all."""
    if ask <= 0 or ask >= 1 or fair <= ask:
        return 0
    f_star = (fair - ask) / (1 - ask)
    stake = min(f_star * kelly_fraction * bankroll_usd, max_notional_usd)
    return max(1, math.floor(stake / ask))


def entry_signal(fair: float, ask: float, bankroll_usd: float, max_notional_usd: float) -> EntrySignal:
    if not (PRICE_BAND[0] <= ask <= PRICE_BAND[1]):
        return EntrySignal(0, 0.0, f"ask {ask:.2f} outside tradeable band {PRICE_BAND}")
    qty = kelly_qty(fair, ask, bankroll_usd, max_notional_usd)
    if qty == 0:
        return EntrySignal(0, fair - ask, "no positive edge")
    # Fees round up to the cent per ORDER, so a bigger order pays less per
    # contract -- recompute the edge at the size actually being traded.
    edge = fair - ask - fee_per_contract(qty, ask)
    if edge > MAX_PLAUSIBLE_EDGE:
        return EntrySignal(0, edge, f"edge {edge:.3f} exceeds MAX_PLAUSIBLE_EDGE -- probable matching error")
    if edge <= MIN_ENTRY_EDGE:
        return EntrySignal(0, edge, f"edge {edge:.3f} below MIN_ENTRY_EDGE")
    return EntrySignal(qty, edge)


def exit_reason(fair_now: float, bid_now: float, qty: float) -> str | None:
    """Why to close an open YES position now, or None to keep holding.
    `bid_now` is the price a sell would actually get."""
    if bid_now <= 0:
        return None  # no bid, nothing to sell into -- hold to settlement
    remaining_edge = fair_now - bid_now
    if remaining_edge > MAX_PLAUSIBLE_EDGE:
        return f"reference broken: spread {remaining_edge:.3f} > MAX_PLAUSIBLE_EDGE"
    net_bid = bid_now - fee_per_contract(max(1, int(qty)), bid_now)
    if fair_now - net_bid <= EXIT_EDGE:
        return f"converged: bid {bid_now:.2f} (net {net_bid:.3f}) >= fair {fair_now:.3f}"
    return None
