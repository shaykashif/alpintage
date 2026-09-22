"""Public, unauthenticated Polymarket market data (Gamma API). Verified live
against gamma-api.polymarket.com as of 2026-09-21/22 -- no key needed,
generous public rate limits. Two things verified the hard way, live:

- The API caps a page at 100 rows even if you ask for more -- paginate with
  `offset` instead of requesting a bigger `limit`.
- `tag_slug` / `tag` / `sport` query params are silently ignored (they don't
  filter -- confirmed by checking the actual results returned). Filtering by
  slug prefix client-side, after fetching by volume order, is what actually
  works.

outcomes/outcomePrices come back as JSON-encoded STRINGS, not native arrays.
"""
from __future__ import annotations

import json
import re

import httpx

BASE = "https://gamma-api.polymarket.com"


def get_markets(limit: int = 100, offset: int = 0, order: str = "volume24hr", closed: bool = False, timeout: float = 15.0) -> list[dict]:
    r = httpx.get(
        f"{BASE}/markets",
        params={"closed": str(closed).lower(), "limit": limit, "offset": offset, "order": order, "ascending": "false"},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def parse_outcomes(market: dict) -> dict[str, float]:
    """outcomes and outcomePrices are JSON-encoded strings like '["Giants",
    "Rams"]' and '["0.086", "0.914"]' -- parse and zip them into {name: price}."""
    names = json.loads(market["outcomes"])
    prices = json.loads(market["outcomePrices"])
    return {n: float(p) for n, p in zip(names, prices)}


def moneyline_markets(sport_slug_prefix: str, max_offset: int = 1000, page_size: int = 100) -> list[dict]:
    """Full-game moneylines only, e.g. 'nfl-nyg-la-2026-09-22' -- NOT
    '...-spread-home-6pt5', '...-1h-moneyline', or player-prop variants,
    which share the same prefix but aren't the plain game winner market.
    Paginates by volume order across `max_offset` rows looking for matches,
    since the game itself isn't always the highest-volume market for that
    slug family (props and spreads can outdraw it)."""
    pattern = re.compile(rf"^{re.escape(sport_slug_prefix)}[a-z0-9]+-[a-z0-9]+-\d{{4}}-\d{{2}}-\d{{2}}$")
    out = []
    for offset in range(0, max_offset, page_size):
        page = get_markets(limit=page_size, offset=offset)
        if not page:
            break
        for m in page:
            if pattern.match(m.get("slug", "")):
                out.append(m)
    return out
