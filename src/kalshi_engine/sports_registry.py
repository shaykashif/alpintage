"""Registry of the identifiers needed to compare the same sport across
Kalshi, The Odds API, and Polymarket. Verified live on 2026-09-21/22:

- Kalshi series tickers: confirmed to exist via GET /series (category=Sports).
- Odds API sport_keys: confirmed via GET /v4/sports.
- Polymarket slug prefixes: confirmed by paginating GET /markets and looking
  at what's actually live -- note "cfb-" for college football, NOT "ncaaf-".
  NBA and EPL showed zero live moneyline markets this week (preseason /
  early season) -- that's expected to vary week to week, not a bug in the
  prefix. Re-verify a league's prefix if it stops matching anything.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SportConfig:
    label: str
    kalshi_series: str
    odds_sport_key: str
    poly_slug_prefix: str


SPORTS: dict[str, SportConfig] = {
    "nfl": SportConfig("NFL", "KXNFLGAME", "americanfootball_nfl", "nfl-"),
    "nba": SportConfig("NBA", "KXNBAGAME", "basketball_nba", "nba-"),
    "mlb": SportConfig("MLB", "KXMLBGAME", "baseball_mlb", "mlb-"),
    "nhl": SportConfig("NHL", "KXNHLGAME", "icehockey_nhl", "nhl-"),
    "ncaaf": SportConfig("NCAAF", "KXNCAAFGAME", "americanfootball_ncaaf", "cfb-"),
}
