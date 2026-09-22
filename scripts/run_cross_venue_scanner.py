"""Compare Kalshi's moneyline price to The Odds API (sportsbooks, averaged
and de-vigged across every book that quoted the game) and Polymarket for the
SAME real-world game, verified by asking Jev whether each candidate pair
actually describes the same game (see event_match.py for why: the sources
name teams inconsistently). Jev's only job here is that event-identity
check -- it never sets a price or a trade decision.

POC MODE (current default): --paper trades directly on any divergence that
clears MIN_TRADE_EDGE net of fees, with no statistical validation gate.
This is a deliberate scope choice for a quant-club proof of concept, not a
claim that the signal is proven -- we have NOT confirmed Kalshi actually
converges toward the sportsbook/Polymarket price, only that they sometimes
disagree. See the project conversation history for the fuller discussion:
divergence is not the same thing as edge. cross_venue_trust.py and
score_cross_venue.py still exist and still work -- pass --require-trust to
restore the stricter, evidence-gated behavior (needs 30+ settled games
where a venue beats Kalshi's own Brier score before it trades on it).

Still never places a REAL order anywhere, on Kalshi or any other venue,
under any flag.

Costs one Odds API call per league (cheap against the ~500/month free-tier
quota) and real Jev calls (one per date-plausible candidate pair -- fine,
per the project owner, Jev calls are inexpensive).

Usage:
    uv run python scripts/run_cross_venue_scanner.py
    uv run python scripts/run_cross_venue_scanner.py --leagues all --paper
    uv run python scripts/run_cross_venue_scanner.py --paper --require-trust   # stricter mode
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from itertools import permutations
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

from kalshi_engine.cross_venue_trust import venue_trust  # noqa: E402
from kalshi_engine.devig import aggregate_fair_probs  # noqa: E402
from kalshi_engine.event_match import confirm_same_game, dates_close, likely_same_game  # noqa: E402
from kalshi_engine.fees import taker_fee  # noqa: E402
from kalshi_engine.odds_client import get_odds  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.polymarket_client import moneyline_markets, parse_outcomes  # noqa: E402
from kalshi_engine.sports_kalshi import fetch_moneyline_games  # noqa: E402
from kalshi_engine.sports_registry import SPORTS  # noqa: E402
from run_scanner import _already_filled_tickers  # noqa: E402

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "cross_venue.jsonl"

# Net-of-fee edge required before a cross-venue divergence is worth
# paper-trading. This is a POC-appropriate bar (real divergences observed
# so far mostly sit at 1-3%), not a validated edge threshold -- lower this
# and trades happen more often; raise it and they happen less. It applies
# whether or not --require-trust is set.
MIN_TRADE_EDGE = 0.015

# Sanity ceiling: an edge bigger than this is far more likely a matching
# bug than real mispricing -- caught live, a same-team-different-day series
# game got mis-paired and produced a fake 44.5-point "edge" that POC mode
# then traded on before this existed. A liquid moneyline market disagreeing
# with a de-vigged sportsbook consensus by more than this is not impossible,
# but it should be looked at by a person, not auto-traded.
MAX_PLAUSIBLE_EDGE = 0.20

# How close two sources' game timestamps must be to even be considered as
# candidates for the same game, before Jev makes the real call. Wide enough
# to absorb timezone/kickoff-vs-market-close slop for ONE game, narrow
# enough to not span into an adjacent day's game of the same season series
# (the exact bug above) -- Jev's own instructions (event_match.py) are the
# primary defense against that now, this is the secondary one.
DATE_WINDOW_HOURS = 30

TRADE_QTY = 1


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def best_team_assignment(kalshi_probs: dict, other_probs: dict) -> list[tuple[str, str]]:
    """Kalshi, Odds API, and Polymarket all spell team names differently
    ('Chicago' vs 'Chicago Bears' vs 'Bears') -- exact string equality
    basically never matches.

    This is NOT simple word-overlap-per-team: that independently assigns
    each Kalshi team its own best match, which breaks badly on "X vs X
    State" games -- caught live on 'New Mexico' vs 'New Mexico St.': both
    names reduce to the same significant words, so a greedy per-team match
    silently paired Kalshi's 'New Mexico' with Odds API's 'New Mexico
    State Aggies' instead of 'New Mexico Lobos', producing a fake ~60-point
    "divergence" out of two teams that actually agreed within 3 points.

    Fix: try every possible one-to-one assignment between the two small
    outcome sets (2, rarely 3 -- brute force is fine) and keep whichever
    assignment maximizes total character-level similarity across BOTH
    pairs at once. Character similarity (not word-overlap) is what
    correctly favors 'St.'<->'State' over the alternative.

    Returns a list of (kalshi_team, other_team) pairs -- a best-effort
    pairing even for two totally unrelated team lists, since this assumes
    the caller already confirmed (via likely_same_game + Jev) that both
    sides describe the same real game before calling this. It aligns WHICH
    team is which; it doesn't decide whether they're the same game at all.
    Shared with score_cross_venue.py, which needs the same alignment to
    score each venue's own Brier score against the real outcome."""
    k_teams = list(kalshi_probs.keys())
    o_teams = list(other_probs.keys())
    if not k_teams or not o_teams:
        return []

    n = min(len(k_teams), len(o_teams))
    best_pairs, best_score = None, -1.0
    for combo in permutations(o_teams, n):
        score = sum(_name_similarity(k_teams[i], combo[i]) for i in range(n))
        if score > best_score:
            best_score, best_pairs = score, list(zip(k_teams[:n], combo))

    return best_pairs or []


def _align_probs(kalshi_probs: dict, other_probs: dict) -> dict[str, float]:
    """Per-team |kalshi - other| using the alignment above."""
    pairs = best_team_assignment(kalshi_probs, other_probs)
    return {k: abs(kalshi_probs[k] - other_probs[o]) for k, o in pairs}


def _max_divergence(kalshi_probs: dict, other_probs: dict) -> float | None:
    diffs = _align_probs(kalshi_probs, other_probs)
    return max(diffs.values()) if diffs else None


def _maybe_trade(kg, row: dict, broker: PaperBroker, already_filled: set[str], require_trust: bool = False) -> None:
    """Paper-buy YES on a Kalshi team if the net-of-fee edge against a
    cross-venue price clears MIN_TRADE_EDGE. Jev's role ends at confirming
    this is the same game (done earlier, in scan_league) -- it has no say
    in this decision.

    require_trust=False (the default, POC mode): trades on the edge alone.
    require_trust=True: also requires cross_venue_trust.venue_trust() to
    say this venue has beaten Kalshi's own Brier score on 30+ settled
    games -- the stricter mode, currently a no-op in practice since 0 games
    are scored yet."""
    for venue_key, probs_key in (("odds_api", "odds_api_avg_probs"), ("polymarket", "polymarket_probs")):
        venue_probs = row.get(probs_key)
        if not venue_probs:
            continue

        note = "POC mode (no statistical validation)"
        if require_trust:
            trusted, reason = venue_trust(venue_key)
            if not trusted:
                continue
            note = f"trusted: {reason}"

        for k_team, o_team in best_team_assignment(kg.team_probs, venue_probs):
            ticker = kg.team_tickers.get(k_team)
            ask = kg.team_asks.get(k_team)
            if ticker is None or ask is None or ticker in already_filled:
                continue
            fee = float(taker_fee(TRADE_QTY, ask))
            edge = venue_probs[o_team] - ask - fee
            if edge > MAX_PLAUSIBLE_EDGE:
                print(
                    f"  SKIPPING {ticker}: edge {edge:.3f} exceeds MAX_PLAUSIBLE_EDGE "
                    f"({MAX_PLAUSIBLE_EDGE}) -- almost certainly a matching error, not "
                    f"real mispricing. Not trading. Check this row in cross_venue.jsonl by hand."
                )
                continue
            if edge > MIN_TRADE_EDGE:
                broker.buy(
                    ticker, "yes", ask, qty=TRADE_QTY,
                    reason=f"cross-venue stat-arb: {venue_key} ({note}), edge={edge:.3f}",
                )
                already_filled.add(ticker)


def scan_league(league_key: str, broker: PaperBroker | None = None, already_filled: set[str] | None = None, require_trust: bool = False) -> list[dict]:
    cfg = SPORTS[league_key]
    print(f"\n=== {cfg.label} ===")

    kalshi_games = fetch_moneyline_games(cfg.kalshi_series)
    print(f"Kalshi: {len(kalshi_games)} open moneyline games")

    try:
        odds_events, quota = get_odds(cfg.odds_sport_key, regions="us", markets="h2h")
        print(f"Odds API: {len(odds_events)} events (quota remaining: {quota.requests_remaining})")
    except Exception as exc:  # noqa: BLE001
        print(f"Odds API: failed ({exc}), skipping this league's sportsbook comparison")
        odds_events = []

    poly_markets = moneyline_markets(cfg.poly_slug_prefix)
    print(f"Polymarket: {len(poly_markets)} moneyline markets")

    rows = []
    for kg in kalshi_games:
        odds_match = None
        for e in odds_events:
            if not dates_close(kg.scheduled_date, e.get("commence_time"), max_hours=DATE_WINDOW_HOURS):
                continue
            label = f"{e.get('away_team')} @ {e.get('home_team')}"
            candidate_text = f"{label}, kickoff {e.get('commence_time')}"
            if not likely_same_game(kg.rules_text, candidate_text):
                continue  # no shared team-name word -- skip the Jev call entirely
            candidate = confirm_same_game("Kalshi", kg.rules_text, "Odds API", candidate_text)
            if candidate.confirmed:
                odds_match = e
                break

        poly_match = None
        for pm in poly_markets:
            if not dates_close(kg.scheduled_date, pm.get("endDate"), max_hours=DATE_WINDOW_HOURS):
                continue
            candidate_text = f"{pm.get('question')} (slug: {pm.get('slug')})"
            if not likely_same_game(kg.rules_text, candidate_text):
                continue
            candidate = confirm_same_game("Kalshi", kg.rules_text, "Polymarket", candidate_text)
            if candidate.confirmed:
                poly_match = pm
                break

        if odds_match is None and poly_match is None:
            continue

        row = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "league": league_key,
            "kalshi_event_ticker": kg.event_ticker,
            "kalshi_probs": kg.team_probs,
            "odds_api_avg_probs": None,
            "odds_api_per_book": None,
            "polymarket_probs": None,
        }

        if odds_match:
            avg, per_book = aggregate_fair_probs(odds_match.get("bookmakers", []))
            row["odds_api_avg_probs"] = avg
            row["odds_api_per_book"] = per_book

        if poly_match:
            row["polymarket_probs"] = parse_outcomes(poly_match)
            row["polymarket_slug"] = poly_match.get("slug")

        row["max_diff_vs_odds_api"] = _max_divergence(kg.team_probs, row["odds_api_avg_probs"] or {})
        row["max_diff_vs_polymarket"] = _max_divergence(kg.team_probs, row["polymarket_probs"] or {})

        if broker is not None:
            _maybe_trade(kg, row, broker, already_filled or set(), require_trust=require_trust)

        label = kg.rules_text.split(", then")[0].replace("If ", "").split(" wins the ")[-1]
        print(f"  {label}: Kalshi={kg.team_probs}")
        if row["odds_api_avg_probs"]:
            n_books = len(row["odds_api_per_book"])
            print(f"    Odds API ({n_books} books, avg de-vigged)={ {k: round(v,3) for k,v in row['odds_api_avg_probs'].items()} } diff={row['max_diff_vs_odds_api']:.3f}" if row["max_diff_vs_odds_api"] is not None else f"    Odds API: {row['odds_api_avg_probs']} (no matching team name)")
        if row["polymarket_probs"]:
            print(f"    Polymarket={row['polymarket_probs']} diff={row['max_diff_vs_polymarket']:.3f}" if row["max_diff_vs_polymarket"] is not None else f"    Polymarket: {row['polymarket_probs']} (no matching team name)")

        rows.append(row)

    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="nfl", help="comma-separated league keys, or 'all'")
    ap.add_argument("--paper", action="store_true", help="paper-trade divergences that clear MIN_TRADE_EDGE (POC mode by default -- see module docstring)")
    ap.add_argument("--require-trust", action="store_true", help="stricter mode: only trade a venue once cross_venue_trust.py says it has earned it")
    args = ap.parse_args()

    load_dotenv()
    leagues = list(SPORTS) if args.leagues == "all" else [s.strip() for s in args.leagues.split(",")]

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    broker = None
    already_filled: set[str] = set()
    if args.paper:
        broker = PaperBroker()
        already_filled = _already_filled_tickers()
        if args.require_trust:
            for venue_key in ("odds_api", "polymarket"):
                trusted, reason = venue_trust(venue_key)
                print(f"[trust check] {venue_key}: {'TRUSTED' if trusted else 'not trusted'} -- {reason}")
        else:
            print("[POC mode] trading on divergence alone (no statistical validation) -- pass --require-trust for the stricter gate")

    all_rows = []
    for league_key in leagues:
        if league_key not in SPORTS:
            print(f"unknown league '{league_key}', skipping (known: {list(SPORTS)})")
            continue
        rows = scan_league(league_key, broker=broker, already_filled=already_filled, require_trust=args.require_trust)
        all_rows.extend(rows)

    with LOG_PATH.open("a", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row) + "\n")

    print(f"\nlogged {len(all_rows)} cross-venue comparison(s) to {LOG_PATH}")

    if broker is not None:
        print(f"\npaper cash remaining: ${broker.cash_usd:.2f}")
        print(f"open paper positions: {len(broker.positions)}")

    ranked = sorted(
        (r for r in all_rows if r["max_diff_vs_odds_api"] or r["max_diff_vs_polymarket"]),
        key=lambda r: max(r["max_diff_vs_odds_api"] or 0, r["max_diff_vs_polymarket"] or 0),
        reverse=True,
    )
    if ranked:
        print("\nbiggest disagreements this run:")
        for r in ranked[:10]:
            diff = max(r["max_diff_vs_odds_api"] or 0, r["max_diff_vs_polymarket"] or 0)
            print(f"  {r['league'].upper()} {r['kalshi_event_ticker']}: max diff {diff:.3f} -- Kalshi={r['kalshi_probs']}")


if __name__ == "__main__":
    main()
