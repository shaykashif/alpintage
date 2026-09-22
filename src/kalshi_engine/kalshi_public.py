"""Unauthenticated Kalshi market-data client, and a filter for text/event-shaped
markets (as opposed to numeric threshold ladders like temperature or commodity
strikes, which have no text for a classification model to read).

Verify BASE and field names against Kalshi's current docs/openapi.yaml before
trusting this for anything beyond the POC. GET /markets needs no auth.
"""
from __future__ import annotations

import httpx

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# strike_type values that mean "this market is a numeric threshold", not a
# text/event judgment. Anything else (e.g. "structured", "custom") is a
# candidate for the text/event angle. Verify against real data if Kalshi
# adds new strike types.
NUMERIC_STRIKE_TYPES = {"greater", "less", "between"}


class PublicClient:
    def __init__(self, base: str = BASE, timeout: float = 10.0):
        self.http = httpx.Client(base_url=base, timeout=timeout)

    def markets(self, max_pages: int = 10, **params) -> list[dict]:
        """Paginate GET /markets. params passed straight through as query params
        (e.g. status="open", mve_filter="exclude", series_ticker="KXNFLRACE")."""
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            q = {"limit": 200, **params}
            if cursor:
                q["cursor"] = cursor
            r = self.http.get("/markets", params=q)
            r.raise_for_status()
            body = r.json()
            out += body["markets"]
            cursor = body.get("cursor")
            if not cursor:
                break
        return out

    def market(self, ticker: str) -> dict:
        r = self.http.get(f"/markets/{ticker}")
        r.raise_for_status()
        return r.json()["market"]


def is_text_shaped(market: dict) -> bool:
    """True if this market has no numeric strike, i.e. it resolves on a
    described event/condition rather than "is X above/below/between N"."""
    if market.get("floor_strike") or market.get("cap_strike"):
        return False
    if market.get("strike_type") in NUMERIC_STRIKE_TYPES:
        return False
    return bool(market.get("rules_primary"))


def build_state_text(market: dict) -> str:
    """The text handed to the classifier as 'state'. Deliberately excludes any
    price/quote field — the whole point is testing whether Jev's judgment,
    formed only from the market's own description, says anything the market
    price doesn't already say."""
    parts = [
        market.get("yes_sub_title") or "",
        market.get("rules_primary") or "",
        market.get("rules_secondary") or "",
    ]
    return "\n\n".join(p for p in parts if p).strip()


def market_implied_prob(market: dict) -> float | None:
    """Mid of yes_bid/yes_ask as the market's implied YES probability. None if
    the book is empty on either side (no real quote to compare against)."""
    bid = market.get("yes_bid_dollars")
    ask = market.get("yes_ask_dollars")
    if bid is None or ask is None:
        return None
    bid, ask = float(bid), float(ask)
    if bid <= 0 or ask >= 1:
        return None
    return (bid + ask) / 2
