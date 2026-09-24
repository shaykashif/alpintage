"""Relationship arbitrage scanner for short-dated event markets (Kalshi +
Polymarket, non-sports). See kalshi_engine/relations.py for the logic.

Each run:
  1. Fetch every non-sports market on both venues that closes AND is
     expected to settle within --horizon-days.
  2. Pick candidate pairs by shared distinctive title words (cheap, no Jev).
  3. Ask Jev -- two calls per pair (both A/B orders; the lower answer
     wins), each asking four relation questions (A=>B, B=>A, mutually
     exclusive, exhaustive) plus a "same underlying?" gate -- reading both
     markets' full rules, settlement sources and dates. Verdicts are cached
     in data/relations_cache.jsonl keyed on both tickers AND a hash of both
     rulebooks, so a pair is only re-asked if its rules text changes. Jev is the only judge of the
     relation -- no human review, by the project owner's choice (paper
     trading). Safeguards that remain: the gate (>= 0.90) and relation
     (>= 0.80) bars, calibrated on hand-labeled live pairs; internal
     consistency of the answers; mock answers never trade; and an
     implausibly large "guaranteed" edge is logged but not traded.
  4. Price every confirmed relation at CURRENT asks and top-of-book size,
     plus Kalshi's own mutually-exclusive events (no Jev needed).
  5. Log every priced opportunity to data/relation_arbs.jsonl, and with
     --paper, buy every leg of each tradeable one (all legs or none).

Never places a real order anywhere.

Usage:
    uv run python scripts/run_relation_scanner.py                 # scan + log only
    uv run python scripts/run_relation_scanner.py --paper
    uv run python scripts/run_relation_scanner.py --horizon-days 3 --max-new-pairs 200 --paper
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

from kalshi_engine import ledger, relations  # noqa: E402
from kalshi_engine.jev_client import ask_noul_multi  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.relation_sources import fetch_kalshi_contracts, fetch_polymarket_contracts  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA / "relations_cache.jsonl"
ARBS_PATH = DATA / "relation_arbs.jsonl"
SUMMARY_PATH = DATA / "paper_pnl_summary.json"

STRATEGY = "relation_arb"


def load_cache(path: Path = CACHE_PATH) -> dict[str, dict]:
    return {row["key"]: row for row in ledger.load_rows(path) if "key" in row}


def ask_jev(a: relations.Contract, b: relations.Contract) -> dict:
    """Raw probabilities only -- the confidence bar is applied at pricing
    time (verdict_relations), so changing --threshold never needs Jev to be
    asked again."""
    ab = ask_noul_multi(relations.relation_state(a, b), relations.RELATION_QUESTIONS)
    ba = ask_noul_multi(relations.relation_state(b, a), relations.RELATION_QUESTIONS)
    return {
        "key": relations.pair_key(a, b),
        "a": a.ticker, "b": b.ticker,
        # Asked in both orders; the lower answer per relation is what counts.
        "probs": relations.merge_orders(ab.probs, ba.probs),
        "probs_ab": ab.probs,
        "probs_ba": ba.probs,
        "route": "typesafe" if ab.route == ba.route == "typesafe" else "mock",
        "model": ab.model,
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }


def verdict_relations(verdict: dict | None, threshold: float,
                      gate_threshold: float = relations.GATE_THRESHOLD) -> tuple[list[str], str | None]:
    """Relations to act on. A mock verdict is cached (so it isn't re-asked
    every run) but never yields a relation -- it must not be mistaken for a
    judgment."""
    if not verdict:
        return [], None
    if verdict.get("route") != "typesafe":
        return [], "mock Jev (no TYPESAFE_API_KEY)"
    return relations.classify(verdict["probs"], threshold, gate_threshold)


def classify_pairs(pairs, cache: dict, max_new: int, workers: int) -> dict[str, dict]:
    """Cached verdicts for every pair, asking Jev (in parallel) for up to
    `max_new` pairs not seen before. Best-scoring pairs are asked first."""
    todo = [(a, b) for a, b, _ in pairs if relations.pair_key(a, b) not in cache][:max_new]
    if todo:
        print(f"asking Jev about {len(todo)} new pair(s) ({workers} in parallel)...")
    new_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ask_jev, a, b): (a, b) for a, b in todo}
        for fut in as_completed(futures):
            a, b = futures[fut]
            try:
                row = fut.result()
            except Exception as exc:  # noqa: BLE001 -- one failed call shouldn't sink the run
                print(f"  Jev failed on {a.ticker} / {b.ticker}: {exc}")
                continue
            cache[row["key"]] = row
            new_rows.append(row)
    if new_rows:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row) + "\n")
    return cache


def arb_row(arb: relations.Arb, verdict: dict | None) -> dict:
    return {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "kind": arb.kind,
        "legs": [{"venue": l.contract.venue, "ticker": l.contract.ticker, "side": l.side, "price": l.price,
                  "title": l.contract.title} for l in arb.legs],
        "qty": arb.qty,
        "payout_per_set": arb.payout_per_set,
        "cost_per_set": arb.cost_per_set,
        "edge_per_set": arb.edge_per_set,
        "fees_usd": arb.fees_usd,
        "tradeable": arb.tradeable,
        "reason": arb.reason,
        "jev_probs": verdict["probs"] if verdict else None,
        "traded": False,
    }


def execute(arb: relations.Arb, broker: PaperBroker, held: set[str], verdict: dict | None) -> tuple[bool, str]:
    """Buy every leg or none. A half-filled arb is a directional bet -- the
    exact failure behind the Sep 23 bracket losses -- so every check that
    could veto a leg runs BEFORE the first buy."""
    tickers = [l.contract.ticker for l in arb.legs]
    if any(t in held for t in tickers):
        return False, "already hold a leg"
    notional = sum(l.price * arb.qty for l in arb.legs)
    if notional + arb.fees_usd > broker.cash_usd:
        return False, "insufficient paper cash for the whole set"
    if broker._total_exposure_usd() + notional > broker.limits.max_total_exposure_usd:
        return False, "whole set would breach max_total_exposure_usd"
    if any(l.price * arb.qty > broker.limits.max_order_notional_usd for l in arb.legs):
        return False, "a leg exceeds max_order_notional_usd"
    if broker.limits.kill_switch_path.exists():
        return False, "kill switch active"
    if broker.realized_pnl_today_usd <= -broker.limits.max_daily_loss_usd:
        return False, "daily loss limit reached"

    group = f"{arb.kind}:{'|'.join(tickers)}"
    for leg in arb.legs:
        fee = None if leg.contract.venue == "kalshi" else leg.contract.fee(arb.qty, leg.price)
        broker.buy(
            leg.contract.ticker, leg.side, leg.price, qty=arb.qty, fee_usd=fee,
            reason=f"relation arb ({arb.kind}), edge={arb.edge_per_set:.3f}/set",
            strategy=STRATEGY, venue=leg.contract.venue, relation=arb.kind, arb_group=group,
            edge=arb.edge_per_set, jev_probs=verdict["probs"] if verdict else None,
        )
        held.add(leg.contract.ticker)
    return True, "filled"


def _settled_payouts() -> dict[str, float]:
    if not SUMMARY_PATH.exists():
        return {}
    try:
        summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {p["ticker"]: p.get("payout", 0.0) for p in summary.get("positions", []) if p.get("status") == "settled"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-days", type=float, default=7.0, help="only markets that settle within this many days")
    ap.add_argument("--max-pairs", type=int, default=3000, help="candidate pairs considered per run (best-scoring first)")
    ap.add_argument("--max-new-pairs", type=int, default=400, help="uncached pairs sent to Jev per run")
    ap.add_argument("--workers", type=int, default=8, help="parallel Jev calls")
    ap.add_argument("--threshold", type=float, default=relations.RELATION_THRESHOLD,
                    help="Jev confidence required on the relation itself (applied to cached answers too)")
    ap.add_argument("--gate-threshold", type=float, default=relations.GATE_THRESHOLD,
                    help="Jev confidence required that both markets measure the same underlying thing")
    ap.add_argument("--no-polymarket", action="store_true")
    ap.add_argument("--paper", action="store_true", help="paper-trade every tradeable arb (all legs or none)")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    contracts = fetch_kalshi_contracts(args.horizon_days)
    print(f"Kalshi: {len(contracts)} non-sports markets settling within {args.horizon_days:g} days")
    if not args.no_polymarket:
        try:
            pm = fetch_polymarket_contracts(args.horizon_days)
            print(f"Polymarket: {len(pm)} non-sports Yes/No markets settling within {args.horizon_days:g} days")
            contracts += pm
        except Exception as exc:  # noqa: BLE001
            print(f"Polymarket: failed ({exc}), continuing with Kalshi only")

    pairs = relations.candidate_pairs(contracts, max_pairs=args.max_pairs)
    print(f"{len(pairs)} candidate pair(s) by shared title words")

    cache = classify_pairs(pairs, load_cache(), args.max_new_pairs, args.workers)

    opportunities: list[tuple[relations.Arb, dict | None]] = []
    confirmed = near_miss = inconsistent = 0
    for a, b, _score in pairs:
        verdict = cache.get(relations.pair_key(a, b))
        rels, why_not = verdict_relations(verdict, args.threshold, args.gate_threshold)
        if why_not and why_not.startswith("inconsistent"):
            inconsistent += 1
        if not rels:
            if verdict and verdict.get("route") == "typesafe" and max(verdict["probs"][r] for r in relations.RELATIONS) >= 0.7:
                near_miss += 1
            continue
        confirmed += 1
        print(f"  RELATION {rels}: {a.ticker} <-> {b.ticker}")
        for rel in rels:
            arb = relations.relation_arb(rel, a, b)
            if arb is not None and arb.edge_per_set > 0:
                opportunities.append((arb, verdict))

    by_event: dict[str, list[relations.Contract]] = {}
    for c in contracts:
        if c.venue == "kalshi" and c.event_mutually_exclusive:
            by_event.setdefault(c.event_id, []).append(c)
    for event_contracts in by_event.values():
        arb = relations.me_event_arb(event_contracts)
        if arb is not None and arb.edge_per_set > 0:
            opportunities.append((arb, None))

    broker, held = None, set()
    if args.paper:
        settled = _settled_payouts()
        broker = PaperBroker.from_ledger(settled=settled)
        held = set(broker.positions)
        print(f"[book] cash ${broker.cash_usd:.2f}, {len(held)} open position(s)")

    rows = []
    opportunities.sort(key=lambda o: o[0].edge_per_set, reverse=True)
    for arb, verdict in opportunities:
        row = arb_row(arb, verdict)
        legs_txt = " + ".join(f"{l.side.upper()} {l.contract.ticker} @{l.price:.2f}" for l in arb.legs)
        status = "tradeable" if arb.tradeable else f"skip ({arb.reason})"
        if broker is not None and arb.tradeable:
            ok, why = execute(arb, broker, held, verdict)
            row["traded"], row["trade_note"] = ok, why
            status = "PAPER-TRADED" if ok else f"not traded ({why})"
        print(f"  [{arb.kind}] edge ${arb.edge_per_set:.3f}/set x{arb.qty}: {legs_txt} -- {status}")
        rows.append(row)

    if rows:
        with ARBS_PATH.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    tradeable = sum(1 for arb, _ in opportunities if arb.tradeable)
    print(f"\n{confirmed} pair(s) with a Jev-confirmed relation (>= {args.threshold}); {near_miss} near miss(es) "
          f"(a relation >= 0.70 that didn't clear both bars); {inconsistent} rejected as inconsistent")
    print(f"{len(opportunities)} priced opportunit(ies) with positive edge, {tradeable} tradeable; logged to {ARBS_PATH}")
    if broker is not None:
        print(f"paper cash remaining: ${broker.cash_usd:.2f}")


if __name__ == "__main__":
    main()
