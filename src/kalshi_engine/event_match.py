"""Matches the same real-world game across Kalshi, The Odds API, and
Polymarket. The three sources name teams inconsistently -- Kalshi uses city
only ("Philadelphia"), The Odds API uses full names ("Philadelphia Eagles"),
Polymarket uses mascots/abbreviations in different fields ("Eagles", "PHI").
Rather than hand-build a team-name mapping table (fragile, needs updating
every time a source changes its convention), this uses a cheap date
pre-filter to narrow candidates, then asks Jev's Noul primitive the actual
judgment: do these two descriptions refer to the same real-world game.

This is a same-entity-resolution judgment, not a prediction -- a reasonable
use of a classifier, unlike asking it to price something. Still worth
spot-checking: log every match Jev confirms and sanity-check a few by eye
before trusting this at scale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .jev_client import ask_noul

_WORD_RE = re.compile(r"[A-Za-z]+")
_STOPWORDS = {
    "pro", "football", "basketball", "baseball", "hockey", "game", "the", "vs", "at",
    "wins", "if", "then", "market", "resolves", "yes", "originally", "scheduled",
    "for", "and", "team", "teams", "college",
}

MATCH_INSTRUCTIONS = (
    "Do these two descriptions refer to the exact same real-world sports game "
    "(the same two teams playing each other, on or around the same date)? "
    "Answer based only on whether they describe the same matchup, not on any "
    "price or probability mentioned."
)

MATCH_CONFIDENCE_THRESHOLD = 0.70  # jev_prob above this counts as a confirmed match


@dataclass
class MatchCandidate:
    a_label: str
    b_label: str
    jev_prob: float
    jev_route: str
    confirmed: bool


def _parse_date(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def dates_close(a: str | None, b: str | None, max_hours: float = 36.0) -> bool:
    """True if two ISO timestamps are within max_hours of each other. Loose
    on purpose -- different sources report kickoff vs. market-close time,
    which can differ by hours; this is just a cheap pre-filter before Jev
    makes the real call, not the match decision itself."""
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) <= max_hours * 3600


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 2} - _STOPWORDS


def likely_same_game(a_text: str, b_text: str) -> bool:
    """Cheap pre-filter to run BEFORE spending a Jev call: True if the two
    texts share at least one distinctive word (almost always a team name).
    On a large slate (NCAAF can have 60+ games/week), brute-forcing every
    date-plausible pair through Jev doesn't scale -- this cuts it to
    approximately one real candidate per game in the common case. It's
    deliberately permissive (a shared word doesn't guarantee a match), so
    Jev still makes the actual confirmation -- this only skips obviously
    unrelated pairs, it never confirms a match by itself."""
    return bool(_tokens(a_text) & _tokens(b_text))


def confirm_same_game(a_label: str, a_text: str, b_label: str, b_text: str) -> MatchCandidate:
    """Ask Jev whether two event descriptions are the same game. Costs one
    real Jev call -- only call this on candidates that already passed a date
    pre-filter, not on every possible pair."""
    state = f"DESCRIPTION A ({a_label}):\n{a_text}\n\nDESCRIPTION B ({b_label}):\n{b_text}"
    result = ask_noul(state, MATCH_INSTRUCTIONS)
    return MatchCandidate(
        a_label=a_label,
        b_label=b_label,
        jev_prob=result.prob,
        jev_route=result.route,
        confirmed=result.prob >= MATCH_CONFIDENCE_THRESHOLD,
    )
