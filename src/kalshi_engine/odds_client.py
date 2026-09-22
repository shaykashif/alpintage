"""Client for The Odds API (https://the-odds-api.com -- note the hyphens;
a similarly-named domain without hyphens returned a different base URL and
auth scheme when checked, and was NOT used here).

Endpoint, param names, and response shape verified against
the-odds-api.com/liveapi/guides/v4/ as of 2026-09-21. Re-check before relying
on this beyond testing -- I have not yet run this against a real key.

Free tier quota was not confirmed from the docs page fetched; check
https://the-odds-api.com/ after signup, and the `x-requests-remaining` /
`x-requests-used` response headers this client surfaces on every call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

BASE = "https://api.the-odds-api.com"

# A handful of common sport_key values, from the docs example. There are many
# more (every league The Odds API covers) -- pass any valid key directly,
# this dict is just a memory aid.
SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
}


class OddsApiError(Exception):
    pass


@dataclass
class QuotaInfo:
    requests_remaining: str | None
    requests_used: str | None


def get_odds(
    sport_key: str,
    regions: str = "us",
    markets: str = "h2h",
    odds_format: str = "american",
    bookmakers: str | None = None,
    timeout: float = 15.0,
) -> tuple[list[dict], QuotaInfo]:
    """GET /v4/sports/{sport_key}/odds/. `sport_key` can be a value from
    SPORT_KEYS or any raw key The Odds API accepts. Returns (events, quota) --
    check quota after every call, the free tier runs out fast."""
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise OddsApiError("ODDS_API_KEY not set in the environment (.env)")

    params = {"apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": odds_format}
    if bookmakers:
        params["bookmakers"] = bookmakers

    r = httpx.get(f"{BASE}/v4/sports/{sport_key}/odds/", params=params, timeout=timeout)
    if r.status_code != 200:
        raise OddsApiError(f"HTTP {r.status_code}: {r.text[:300]}")

    quota = QuotaInfo(
        requests_remaining=r.headers.get("x-requests-remaining"),
        requests_used=r.headers.get("x-requests-used"),
    )
    return r.json(), quota
