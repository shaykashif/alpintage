"""Pure-arithmetic mispricing checks. No model, no Jev, nothing probabilistic
-- this is the part of the original plan that doesn't need any of that.

Two checks, both net of taker fees:

1. Ladder (monotonicity): for nested "greater than" thresholds on the same
   underlying (e.g. platinum above $1818, above $1819, ...), a higher strike
   can never be more likely than a lower one. If you can buy YES at the lower
   strike and buy NO at the higher strike for a guaranteed-$1-or-more payout
   costing less than $1 after fees, that's a violation. See the derivation
   in the project conversation history / README -- the tradeable condition is:

       yes_bid(higher strike) > yes_ask(lower strike) + total fees

2. Bracket sum: for a set of mutually exclusive, exhaustive "between" strike
   buckets covering one event (e.g. temperature range brackets), buying one
   YES contract on every bucket guarantees exactly $1 back. Profitable if:

       sum(yes_ask over all buckets) + total fees < $1

Only top-of-book (best bid/ask) is used -- GET /markets gives no depth, and
GET /markets/{ticker}/orderbook needs auth we haven't built. Every candidate
here is sized at 1 contract per leg; treat any "edge" as a best-of-book
estimate, not a guarantee it survives at size.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .fees import taker_fee, total_taker_fee

# Strips a trailing "-<optional T><number>" ticker segment, e.g.
# "...-T1819.49" (platinum) or "...-LARBCORUM24-10" -> "...-LARBCORUM24"
# (an NFL player-prop threshold). What's left identifies the underlying
# subject a ladder must share -- without this, two different players' props
# that happen to land in the same event with the same floor_strike look like
# a nested ladder and are not. Verified against both shapes on live data.
_TRAILING_THRESHOLD = re.compile(r"-T?[0-9]+(?:\.[0-9]+)?$")


def _subject_key(ticker: str) -> str:
    stripped = _TRAILING_THRESHOLD.sub("", ticker)
    return stripped if stripped != ticker else ticker  # no match -> own group


def _num(m: dict, key: str) -> float | None:
    v = m.get(key)
    return None if v in (None, "") else float(v)


def has_two_sided_quote(m: dict) -> bool:
    bid, ask = _num(m, "yes_bid_dollars"), _num(m, "yes_ask_dollars")
    return bid is not None and ask is not None and bid > 0 and ask < 1


def group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for m in markets:
        groups.setdefault(m["event_ticker"], []).append(m)
    return groups


def group_by_subject(markets: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Group by (event_ticker, subject) so a ladder only ever compares
    markets on the same underlying variable, not just the same event."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in markets:
        key = (m["event_ticker"], _subject_key(m["ticker"]))
        groups.setdefault(key, []).append(m)
    return groups


@dataclass
class LadderViolation:
    event_ticker: str
    low_ticker: str  # lower strike -- buy YES here
    high_ticker: str  # higher strike -- buy NO here
    low_strike: float
    high_strike: float
    low_yes_ask: float
    high_yes_bid: float
    fees_usd: float
    edge_usd: float  # guaranteed profit per contract-pair, after fees, best case


def find_ladder_violations(markets: list[dict]) -> list[LadderViolation]:
    """Scan every event for "greater than" ladders and flag adjacent-strike
    monotonicity violations net of fees."""
    out: list[LadderViolation] = []
    for (event_ticker, _subject), group in group_by_subject(markets).items():
        rung = [
            m for m in group
            if m.get("strike_type") == "greater"
            and m.get("floor_strike") is not None
            and has_two_sided_quote(m)
        ]
        if len(rung) < 2:
            continue
        rung.sort(key=lambda m: float(m["floor_strike"]))  # ascending strike
        for low, high in zip(rung, rung[1:]):
            low_ask = _num(low, "yes_ask_dollars")
            high_bid = _num(high, "yes_bid_dollars")
            no_ask_high = round(1 - high_bid, 4)  # buying NO at the higher strike
            leg_fees = total_taker_fee([(1, low_ask), (1, no_ask_high)])
            cost = Decimal(str(low_ask)) + Decimal(str(no_ask_high)) + leg_fees
            edge = Decimal("1.00") - cost  # guaranteed payout is >= $1
            if edge > 0:
                out.append(LadderViolation(
                    event_ticker=event_ticker,
                    low_ticker=low["ticker"],
                    high_ticker=high["ticker"],
                    low_strike=float(low["floor_strike"]),
                    high_strike=float(high["floor_strike"]),
                    low_yes_ask=low_ask,
                    high_yes_bid=high_bid,
                    fees_usd=float(leg_fees),
                    edge_usd=float(edge),
                ))
    return out


@dataclass
class BracketSumViolation:
    event_ticker: str
    tickers: list[str]
    sum_yes_ask: float
    fees_usd: float
    edge_usd: float  # guaranteed profit per contract-set, after fees


def find_bracket_sum_violations(markets: list[dict]) -> list[BracketSumViolation]:
    """Scan every event for "between" (bounded-range) brackets and flag
    buy-all-YES sums that beat $1 after fees.

    Caveat: this assumes the "between" markets in an event are mutually
    exclusive AND exhaustive (they cover the whole range with no gaps or
    overlaps). Kalshi's market objects didn't expose a field confirming that
    when this was written -- verify against the event/series definition
    before trusting a flagged violation, especially a large one. A large
    "edge" here is much more likely to mean the assumption is wrong than
    that free money exists.
    """
    out: list[BracketSumViolation] = []
    for event_ticker, group in group_by_event(markets).items():
        buckets = [
            m for m in group
            if m.get("strike_type") == "between"
            and m.get("floor_strike") is not None
            and m.get("cap_strike") is not None
            and has_two_sided_quote(m)
        ]
        if len(buckets) < 2:
            continue
        asks = [_num(m, "yes_ask_dollars") for m in buckets]
        leg_fees = total_taker_fee([(1, a) for a in asks])
        cost = sum((Decimal(str(a)) for a in asks), Decimal("0.00")) + leg_fees
        edge = Decimal("1.00") - cost
        if edge > 0:
            out.append(BracketSumViolation(
                event_ticker=event_ticker,
                tickers=[m["ticker"] for m in buckets],
                sum_yes_ask=float(sum(asks)),
                fees_usd=float(leg_fees),
                edge_usd=float(edge),
            ))
    return out
