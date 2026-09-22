"""Groups Kalshi's per-team moneyline markets (e.g. KXNFLGAME) into one
row per game, since each Kalshi game is two separate markets (one per team)
sharing an event_ticker.

Important: `close_time` is NOT the game's kickoff time -- verified live it
trails the actual game date by several days (it's the market's trading
deadline, not the event date). The actual scheduled date is only in the
rules text ("...originally scheduled for Sep 28, 2026"), so that's what
event_match.py's date pre-filter should use, not close_time."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .kalshi_public import PublicClient, market_implied_prob

_SCHEDULED_RE = re.compile(r"scheduled for ([A-Za-z]+ \d{1,2}, \d{4})")


def extract_scheduled_date(rules_text: str) -> str | None:
    """Pulls the real game date out of rules_primary text, e.g. 'Sep 28,
    2026' -> '2026-09-28T00:00:00+00:00'. Returns None if the pattern isn't
    found -- verify this regex still matches if Kalshi changes its wording."""
    m = _SCHEDULED_RE.search(rules_text)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%b %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.isoformat()


@dataclass
class KalshiGame:
    event_ticker: str
    close_time: str | None
    scheduled_date: str | None  # the actual game date, parsed from rules text
    team_probs: dict[str, float]  # team name (Kalshi's yes_sub_title) -> mid of yes_bid/yes_ask
    team_asks: dict[str, float]  # team name -> yes_ask_dollars -- the real price a buy would pay
    team_tickers: dict[str, str]  # team name -> market ticker
    rules_text: str  # rules_primary from either team's market -- names both teams


def fetch_moneyline_games(series_ticker: str, max_pages: int = 5) -> list[KalshiGame]:
    client = PublicClient()
    markets = client.markets(max_pages=max_pages, status="open", series_ticker=series_ticker, mve_filter="exclude")

    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)

    games = []
    for event_ticker, legs in by_event.items():
        if len(legs) != 2:
            continue  # a plain moneyline game has exactly two team-markets
        team_probs, team_asks, team_tickers = {}, {}, {}
        for m in legs:
            prob = market_implied_prob(m)
            if prob is None:
                continue
            team = m.get("yes_sub_title") or m["ticker"]
            team_probs[team] = prob
            team_asks[team] = float(m["yes_ask_dollars"])
            team_tickers[team] = m["ticker"]
        if len(team_probs) != 2:
            continue  # skip games with an empty book on either side
        rules_text = legs[0].get("rules_primary", "")
        games.append(KalshiGame(
            event_ticker=event_ticker,
            close_time=legs[0].get("close_time"),
            scheduled_date=extract_scheduled_date(rules_text),
            team_probs=team_probs,
            team_asks=team_asks,
            team_tickers=team_tickers,
            rules_text=rules_text,
        ))
    return games
