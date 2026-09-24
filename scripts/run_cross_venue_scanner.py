"""Compare Kalshi's moneyline price to The Odds API (sportsbooks, averaged
and de-vigged across every book that quoted the game) and Polymarket for the
SAME real-world game, verified by asking Jev whether each candidate pair
actually describes the same game (see event_match.py for why: the sources
name teams inconsistently), and then asking Jev again whether the proposed
team-to-team pairing within that game is right (name similarity only
proposes it). Jev's only jobs here are those two identity checks -- it
never sets a price or a trade decision.

--paper runs this as a paper statistical-arbitrage book (logic in
kalshi_engine/stat_arb.py): ENTER when Kalshi's YES ask sits below a
cross-venue fair value by more than fees, sized by quarter-Kelly; EXIT on a
later scan when Kalshi's bid has converged to the fair value (or the spread
blows out implausibly); otherwise hold to settlement. Positions persist
across runs via data/paper_fills.jsonl (kalshi_engine/ledger.py).

POC MODE (current default): entries trade directly on any divergence that
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

from kalshi_engine import ledger, stat_arb  # noqa: E402
from kalshi_engine.cross_venue_trust import venue_trust  # noqa: E402
from kalshi_engine.devig import aggregate_fair_probs  # noqa: E402
from kalshi_engine.event_match import confirm_same_game, confirm_team_pairing, dates_close, likely_same_game  # noqa: E402
from kalshi_engine.odds_client import get_odds  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.polymarket_client import moneyline_markets, parse_outcomes  # noqa: E402
from kalshi_engine.sports_kalshi import fetch_moneyline_games  # noqa: E402
from kalshi_engine.sports_registry import SPORTS  # noqa: E402

LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "cross_venue.jsonl"
SUMMARY_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_pnl_summary.json"

# Entry/exit thresholds, Kelly sizing and the tradeable price band live in
# kalshi_engine/stat_arb.py (pure logic, unit-tested there). MIN_ENTRY_EDGE
# stays at the owner's 0.005 demo setting; MAX_PLAUSIBLE_EDGE (0.20) is the
# matching-bug sanity ceiling -- caught live, a same-team-different-day
# series game got mis-paired and produced a fake 44.5-point "edge".
# Re-exported here so callers that read them off this module keep working.
MIN_TRADE_EDGE = stat_arb.MIN_ENTRY_EDGE
MAX_PLAUSIBLE_EDGE = stat_arb.MAX_PLAUSIBLE_EDGE

VENUES = (("odds_api", "odds_api_avg_probs"), ("polymarket", "polymarket_probs"))

# How close two sources' game timestamps must be to even be considered as
# candidates for the same game, before Jev makes the real call. Wide enough
# to absorb timezone/kickoff-vs-market-close slop for ONE game, narrow
# enough to not span into an adjacent day's game of the same season series
# (the exact bug above) -- Jev's own instructions (event_match.py) are the
# primary defense against that now, this is the secondary one.
DATE_WINDOW_HOURS = 30


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
    score each venue's own Brier score against the real outcome (as a
    fallback for rows logged before Jev verified the pairing -- see
    verified_team_map)."""
    ranked = ranked_team_assignments(kalshi_probs, other_probs)
    return ranked[0] if ranked else []


def ranked_team_assignments(kalshi_probs: dict, other_probs: dict) -> list[list[tuple[str, str]]]:
    """Every one-to-one assignment, best total name similarity first."""
    k_teams = list(kalshi_probs.keys())
    o_teams = list(other_probs.keys())
    if not k_teams or not o_teams:
        return []
    n = min(len(k_teams), len(o_teams))
    scored = [
        (sum(_name_similarity(k_teams[i], combo[i]) for i in range(n)), list(zip(k_teams[:n], combo)))
        for combo in permutations(o_teams, n)
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [pairs for _, pairs in scored]


# How many candidate pairings (best similarity first) Jev is asked about
# before giving up on a venue for this game. 2 covers every two-team game.
MAX_PAIRING_CHECKS = 2


def verified_team_map(kg, venue_label: str, venue_text: str, venue_probs: dict) -> tuple[dict[str, str] | None, float | None]:
    """{kalshi_team: venue_team}, confirmed by Jev -- or (None, prob) if Jev
    confirms none of the top candidate pairings. Name similarity only
    PROPOSES the pairing now; Jev decides. A rejected best guess gets the
    next candidate (for a two-team game, the swap) before the venue is
    dropped for this game."""
    last_prob = None
    for pairs in ranked_team_assignments(kg.team_probs, venue_probs)[:MAX_PAIRING_CHECKS]:
        check = confirm_team_pairing("Kalshi", kg.rules_text, venue_label, venue_text, pairs)
        last_prob = check.jev_prob
        if check.confirmed:
            return dict(pairs), check.jev_prob
    return None, last_prob


def _max_divergence(kalshi_probs: dict, other_probs: dict, team_map: dict[str, str] | None) -> float | None:
    """Largest per-team |kalshi - other| under a Jev-verified team map."""
    if not team_map or not other_probs:
        return None
    return max(abs(kalshi_probs[k] - other_probs[o]) for k, o in team_map.items())


def _reference_fairs(kg, row: dict) -> dict[str, dict[str, float]]:
    """{venue: {kalshi_team: that venue's fair prob for the same team}},
    using ONLY the team map Jev verified this cycle (row[f"{venue}_team_map"]).
    A venue with no verified map is left out -- never traded or exited on."""
    out = {}
    for venue_key, probs_key in VENUES:
        venue_probs = row.get(probs_key)
        team_map = row.get(f"{venue_key}_team_map")
        if not venue_probs or not team_map:
            continue
        out[venue_key] = {k: venue_probs[o] for k, o in team_map.items()}
    return out


def _maybe_trade(kg, row: dict, broker: PaperBroker, already_filled: set[str], require_trust: bool = False) -> None:
    """Open a stat-arb position: paper-buy YES on a Kalshi team when the
    net-of-fee edge against a cross-venue fair value clears the entry bar
    (stat_arb.entry_signal), sized by fractional Kelly. Jev's role ends at
    confirming this is the same game (done earlier, in scan_league) -- it
    has no say in this decision.

    `already_filled` is the set of tickers currently held (plus anything
    bought earlier this run): no pyramiding into an open position. A
    ticker exited on convergence can be re-entered if the spread reopens.

    require_trust=False (the default, POC mode): trades on the edge alone.
    require_trust=True: also requires cross_venue_trust.venue_trust() to
    say this venue has beaten Kalshi's own Brier score on 30+ settled
    games -- the stricter mode."""
    for venue_key, fairs in _reference_fairs(kg, row).items():
        note = "POC mode (no statistical validation)"
        if require_trust:
            trusted, reason = venue_trust(venue_key)
            if not trusted:
                continue
            note = f"trusted: {reason}"

        for k_team, fair in fairs.items():
            ticker = kg.team_tickers.get(k_team)
            ask = kg.team_asks.get(k_team)
            if ticker is None or ask is None or ticker in already_filled:
                continue
            sig = stat_arb.entry_signal(fair, ask, broker.cash_usd, broker.limits.max_order_notional_usd)
            if sig.qty == 0:
                if sig.edge > MAX_PLAUSIBLE_EDGE:
                    print(f"  SKIPPING {ticker}: {sig.reason}. Not trading. Check this row in cross_venue.jsonl by hand.")
                continue
            fill = broker.buy(
                ticker, "yes", ask, qty=sig.qty,
                reason=f"cross-venue stat-arb: {venue_key} ({note}), edge={sig.edge:.3f}",
                strategy=ledger.STAT_ARB_STRATEGY, venue=venue_key, fair=round(fair, 4),
                edge=round(sig.edge, 4), event_ticker=getattr(kg, "event_ticker", None),
            )
            if fill is not None:
                print(f"  ENTER {ticker}: {sig.qty} @ {ask:.2f} vs {venue_key} fair {fair:.3f} (edge {sig.edge:.3f})")
                already_filled.add(ticker)


def _maybe_exit(kg, row: dict, broker: PaperBroker, held: dict[str, ledger.Position]) -> None:
    """Close any held stat-arb position on this game whose spread has
    converged (or blown out), using THIS cycle's prices. Prefers the venue
    the position was opened against; falls back to any venue quoting it."""
    fairs_by_venue = _reference_fairs(kg, row)
    for k_team, ticker in kg.team_tickers.items():
        pos = held.get(ticker)
        if pos is None or ticker not in broker.positions:
            continue
        venue = pos.meta.get("venue")
        if k_team not in fairs_by_venue.get(venue, {}):
            venue = next((v for v, f in fairs_by_venue.items() if k_team in f), None)
        bid = getattr(kg, "team_bids", {}).get(k_team)
        if venue is None or bid is None:
            continue
        fair_now = fairs_by_venue[venue][k_team]
        why = stat_arb.exit_reason(fair_now, bid, broker.positions[ticker]["qty"])
        if why is None:
            continue
        fill = broker.sell(
            ticker, bid, reason=f"cross-venue stat-arb exit ({venue}): {why}",
            strategy=ledger.STAT_ARB_STRATEGY, venue=venue, fair=round(fair_now, 4),
            event_ticker=getattr(kg, "event_ticker", None),
        )
        if fill is not None:
            print(f"  EXIT {ticker}: {fill.qty:g} @ {bid:.2f} -- {why}")


def scan_league(league_key: str, broker: PaperBroker | None = None, already_filled: set[str] | None = None,
                require_trust: bool = False, held: dict[str, ledger.Position] | None = None) -> list[dict]:
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
        # Deliberately NOT skipping games already in cross_venue.jsonl: a
        # game is re-evaluated with fresh prices every cycle on purpose,
        # since a real divergence can develop closer to game time, not
        # necessarily at first sighting. Skipping re-evaluation here was
        # an earlier over-correction for the rate-limit bug -- that bug was
        # actually in score_cross_venue.py's per-ROW (not per-ticker) Kalshi
        # calls with no pacing, which is fixed there independently of this.
        # This does mean cross_venue.jsonl grows every cycle per game seen,
        # by design -- cheap to store, and it's also the spread history a
        # convergence study needs. already_filled (below) still prevents
        # adding to a ticker while it's held.
        odds_match, odds_text = None, None
        for e in odds_events:
            if not dates_close(kg.scheduled_date, e.get("commence_time"), max_hours=DATE_WINDOW_HOURS):
                continue
            label = f"{e.get('away_team')} @ {e.get('home_team')}"
            candidate_text = f"{label}, kickoff {e.get('commence_time')}"
            if not likely_same_game(kg.rules_text, candidate_text):
                continue  # no shared team-name word -- skip the Jev call entirely
            candidate = confirm_same_game("Kalshi", kg.rules_text, "Odds API", candidate_text)
            if candidate.confirmed:
                odds_match, odds_text = e, candidate_text
                break

        poly_match, poly_text = None, None
        for pm in poly_markets:
            if not dates_close(kg.scheduled_date, pm.get("endDate"), max_hours=DATE_WINDOW_HOURS):
                continue
            candidate_text = f"{pm.get('question')} (slug: {pm.get('slug')})"
            if not likely_same_game(kg.rules_text, candidate_text):
                continue
            candidate = confirm_same_game("Kalshi", kg.rules_text, "Polymarket", candidate_text)
            if candidate.confirmed:
                poly_match, poly_text = pm, candidate_text
                break

        if odds_match is None and poly_match is None:
            continue

        row = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "league": league_key,
            "kalshi_event_ticker": kg.event_ticker,
            "kalshi_probs": kg.team_probs,
            # Tradeable quotes, not just the mid -- what a convergence study
            # (did the spread actually close?) needs to replay entries/exits.
            "kalshi_asks": kg.team_asks,
            "kalshi_bids": kg.team_bids,
            "odds_api_avg_probs": None,
            "odds_api_per_book": None,
            "polymarket_probs": None,
            # Jev-verified {kalshi_team: venue_team}; None = not quoted, or
            # Jev confirmed no pairing (then that venue is ignored below).
            "odds_api_team_map": None,
            "polymarket_team_map": None,
        }

        if odds_match:
            avg, per_book = aggregate_fair_probs(odds_match.get("bookmakers", []))
            row["odds_api_avg_probs"] = avg
            row["odds_api_per_book"] = per_book
            if avg:
                row["odds_api_team_map"], row["odds_api_pairing_jev_prob"] = verified_team_map(kg, "Odds API", odds_text, avg)

        if poly_match:
            row["polymarket_probs"] = parse_outcomes(poly_match)
            row["polymarket_slug"] = poly_match.get("slug")
            if row["polymarket_probs"]:
                row["polymarket_team_map"], row["polymarket_pairing_jev_prob"] = verified_team_map(
                    kg, "Polymarket", poly_text, row["polymarket_probs"])

        for venue_key, probs_key in VENUES:
            if row[probs_key] and row[f"{venue_key}_team_map"] is None:
                print(f"  {kg.event_ticker}: Jev did not confirm any {venue_key} team pairing -- ignoring that venue for this game")

        row["max_diff_vs_odds_api"] = _max_divergence(kg.team_probs, row["odds_api_avg_probs"], row["odds_api_team_map"])
        row["max_diff_vs_polymarket"] = _max_divergence(kg.team_probs, row["polymarket_probs"], row["polymarket_team_map"])

        if broker is not None:
            # Exits first: closing a converged position frees risk budget.
            _maybe_exit(kg, row, broker, held or {})
            _maybe_trade(kg, row, broker, already_filled if already_filled is not None else set(), require_trust=require_trust)

        label = kg.rules_text.split(", then")[0].replace("If ", "").split(" wins the ")[-1]
        print(f"  {label}: Kalshi={kg.team_probs}")
        if row["odds_api_avg_probs"]:
            n_books = len(row["odds_api_per_book"])
            print(f"    Odds API ({n_books} books, avg de-vigged)={ {k: round(v,3) for k,v in row['odds_api_avg_probs'].items()} } diff={row['max_diff_vs_odds_api']:.3f}" if row["max_diff_vs_odds_api"] is not None else f"    Odds API: {row['odds_api_avg_probs']} (no matching team name)")
        if row["polymarket_probs"]:
            print(f"    Polymarket={row['polymarket_probs']} diff={row['max_diff_vs_polymarket']:.3f}" if row["max_diff_vs_polymarket"] is not None else f"    Polymarket: {row['polymarket_probs']} (no matching team name)")

        rows.append(row)

    return rows


def _settled_payouts() -> dict[str, float]:
    """ticker -> settlement payout from score_paper_fills.py's cached
    summary. Settlement isn't a ledger row, so this is how a replayed
    broker knows a position has already closed."""
    if not SUMMARY_PATH.exists():
        return {}
    try:
        summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {p["ticker"]: p.get("payout", 0.0) for p in summary.get("positions", []) if p.get("status") == "settled"}


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
    held: dict[str, ledger.Position] = {}
    if args.paper:
        # Replay the ledger so cash, exposure limits and exits see every
        # position still open from past runs, not a fresh $1000 book.
        settled = _settled_payouts()
        broker = PaperBroker.from_ledger(settled=settled)
        held = {
            t: p for t, p in ledger.open_positions(ledger.load_rows(), exclude_tickers=set(settled)).items()
            if p.strategy == ledger.STAT_ARB_STRATEGY
        }
        already_filled = set(broker.positions)
        print(f"[book] cash ${broker.cash_usd:.2f}, {len(broker.positions)} open position(s), {len(held)} stat-arb")
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
        rows = scan_league(league_key, broker=broker, already_filled=already_filled, require_trust=args.require_trust, held=held)
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
