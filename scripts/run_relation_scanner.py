"""Relationship arbitrage scanner for short-dated CULTURAL, ECONOMIC and
GEOPOLITICAL event markets (Kalshi + Polymarket; category allowlists in relation_sources.py).
See kalshi_engine/relations.py for the logic.

Each run:
  1. Fetch every market on both venues that closes AND is expected to
     settle within --horizon-days, and keep only CULTURAL, ECONOMIC and
     GEOPOLITICAL events (topics.py: rules for clear-cut categories, one cached Jev
     call per grey-area event).
  2. Pick candidate pairs by shared distinctive title words (cheap, no Jev).
  3. Ask Jev -- two calls per pair (both A/B orders; the lower answer
     wins), each asking four relation questions (A=>B, B=>A, mutually
     exclusive, exhaustive) plus a "same underlying?" gate -- reading both
     markets' full rules, settlement sources and dates. Verdicts are cached
     in data/relations_cache.jsonl keyed on both tickers AND a hash of both
     rulebooks, so a pair is only re-asked if its rules text changes. Jev
     is the only judge of the relation -- no human review, by the project
     owner's choice (paper trading). Safeguards that remain: the gate (>= 0.90, or >= 0.80
     when the only relations are implications) and relation
     (>= 0.80) bars, calibrated on hand-labeled live pairs; internal
     consistency of the answers; mock answers never trade; and an
     implausibly large "guaranteed" edge is logged but not traded.
  4. Price every confirmed relation at CURRENT asks and top-of-book size,
     plus Kalshi's own mutually-exclusive events (no Jev needed).
  5. Log every priced opportunity to data/relation_arbs.jsonl, and with
     --paper, buy every leg of each tradeable one (all legs or none).
  6. Write data/relation_comparisons.json: every pair Jev judged this run,
     with its verdict and whether prices currently honour it -- what the
     dashboard's comparisons table shows, violations or not.
  7. Write data/relation_watchlist.json: every confirmed relation, which
     run_relation_watcher.py re-prices every few seconds between scans.

Never places a real order anywhere.

Usage:
    uv run python scripts/run_relation_scanner.py                 # scan + log only
    uv run python scripts/run_relation_scanner.py --paper
    uv run python scripts/run_relation_scanner.py --horizon-days 3 --max-new-pairs 200 --paper
"""
from __future__ import annotations

import argparse
import contextlib
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
from kalshi_engine.relation_trading import (  # noqa: E402
    ARBS_PATH, STRATEGY, HeldBook, append_arb_rows, arb_row, execute, price_group, price_relations, settled_payouts,
    write_watchlist,
)
from kalshi_engine.relation_sources import (  # noqa: E402
    fetch_kalshi_events, fetch_polymarket_events, kalshi_contracts, polymarket_contracts, topic_subjects,
)
from kalshi_engine.topics import TopicClassifier  # noqa: E402
from kalshi_engine import structural  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA / "relations_cache.jsonl"
COMPARISONS_PATH = DATA / "relation_comparisons.json"
MAX_COMPARISON_ROWS = 600  # the dashboard ships this file every minute -- keep it bounded

__all__ = ["ARBS_PATH", "STRATEGY", "execute"]  # re-exported: tests and older callers import them from here


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
        "a_title": a.title, "b_title": b.title, "a_venue": a.venue, "b_venue": b.venue,
        # Asked in both orders; the lower answer per relation is what counts.
        "probs": relations.merge_orders(ab.probs, ba.probs),
        "probs_ab": ab.probs,
        "probs_ba": ba.probs,
        "route": "typesafe" if ab.route == ba.route == "typesafe" else "mock",
        "model": ab.model,
        "prompt_version": relations.PROMPT_VERSION,
        "asked_at": datetime.now(timezone.utc).isoformat(),
    }


def verdict_relations(verdict: dict | None, threshold: float,
                      gate_threshold: float = relations.GATE_THRESHOLD,
                      implication_gate_threshold: float = relations.IMPLICATION_GATE_THRESHOLD,
                      ) -> tuple[list[str], str | None]:
    """Relations to act on. A mock verdict is cached (so it isn't re-asked
    every run) but never yields a relation -- it must not be mistaken for a
    judgment."""
    if not verdict:
        return [], None
    if verdict.get("route") != "typesafe":
        return [], "mock Jev (no TYPESAFE_API_KEY)"
    return relations.classify(verdict["probs"], threshold, gate_threshold, implication_gate_threshold)


def needs_reask(verdict: dict | None) -> bool:
    """A cached verdict judged under an older prompt whose accepted
    relations include one the current prompt is stricter about. Pairs it
    currently rejects stay cached -- a stricter question can't accept them."""
    if not verdict or verdict.get("route") != "typesafe":
        return False
    if verdict.get("prompt_version", 1) >= relations.PROMPT_VERSION:
        return False
    rels, _ = relations.classify(verdict["probs"])
    if relations.STRICTER_IN_CURRENT & set(rels):
        return True
    # v3 added same_source/same_subject: a pre-v3 implication that clears the
    # relation bar but not same_underlying might pass on them -- ask again.
    probs = verdict["probs"]
    return "same_source" not in probs and max(probs.get(r, 0.0) for r in relations.IMPLICATIONS) >= relations.RELATION_THRESHOLD


def classify_pairs(pairs, cache: dict, max_new: int, workers: int) -> dict[str, dict]:
    """Cached verdicts for every pair, asking Jev (in parallel) for up to
    `max_new` pairs not seen before. Kalshi x Polymarket pairs go first --
    the same event listed on both venues is the cleanest arbitrage, but
    Kalshi's ~20x larger catalog otherwise buries them (first cross-venue
    pair ranked #407 by title similarity, seen live) -- then best-scoring."""
    stale = [(a, b) for a, b, _ in pairs if needs_reask(cache.get(relations.pair_key(a, b)))]
    uncached = [(a, b) for a, b, _ in pairs if relations.pair_key(a, b) not in cache]
    # Re-asks first: those pairs are confirmed now under an outdated question.
    todo = (stale + sorted(uncached, key=lambda p: p[0].venue == p[1].venue))[:max_new]  # stable: keeps score order
    if todo:
        print(f"asking Jev about {len(todo)} pair(s) ({min(len(stale), max_new)} re-asked under prompt "
              f"v{relations.PROMPT_VERSION}; {workers} in parallel)...")
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
            if len(new_rows) % 100 == 0:  # a first run asks thousands of calls -- show it's alive
                print(f"  ...{len(new_rows)}/{len(todo)} pairs judged", flush=True)
    if new_rows:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row) + "\n")
    return cache


def comparison_row(a: relations.Contract, b: relations.Contract, verdict: dict, rels: list[str],
                   why_not: str | None) -> tuple[dict, list[relations.Arb]]:
    """One judged pair for the dashboard's comparisons table -- shown whether
    or not its prices break anything -- plus its priced arbitrage sets."""
    probs = verdict.get("probs", {})
    best = max(relations.RELATIONS, key=lambda r: probs.get(r, 0.0))
    arbs: list[relations.Arb] = []
    if rels:
        priced = price_relations(a, b, rels)
        status, edge, arbs = priced["status"], priced["edge"], priced["arbs"]
        why_not = priced["note"] or why_not
    else:
        edge = None
        status = "near miss" if probs.get(best, 0.0) >= 0.7 else "unrelated"

    def side(c):
        return {"ticker": c.ticker, "title": c.title, "venue": c.venue, "topic": c.topic}

    return {
        "a": side(a), "b": side(b), "relations": rels, "note": why_not,
        "best": {"relation": best, "prob": round(probs.get(best, 0.0), 3)},
        "gate": round(probs.get("same_underlying", 0.0), 3),
        "status": status, "edge": edge, "asked_at": verdict.get("asked_at"),
    }, arbs


_STATUS_ORDER = {"violation": 0, "consistent": 1, "unpriced": 2, "near miss": 3, "unrelated": 4}


def write_comparisons(rows: list[dict], universe: dict, total_judged: int) -> None:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["cross_venue"] = sum(1 for r in rows if r["a"]["venue"] != r["b"]["venue"])
    rows = sorted(rows, key=lambda r: (_STATUS_ORDER[r["status"]], r["a"]["venue"] == r["b"]["venue"], -r["best"]["prob"]))
    COMPARISONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPARISONS_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "live_pairs": len(rows),
        "total_judged": total_judged,
        "counts": counts,
        "universe": universe,
        "rows": rows[:MAX_COMPARISON_ROWS],
    }), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-days", type=float, default=21.0, help="only markets that settle within this many days")
    ap.add_argument("--max-pairs", type=int, default=20000, help="candidate pairs considered per run (best-scoring first)")
    ap.add_argument("--max-new-pairs", type=int, default=1200, help="uncached pairs sent to Jev per run (cross-venue first)")
    ap.add_argument("--workers", type=int, default=8, help="parallel Jev calls")
    ap.add_argument("--max-new-topics", type=int, default=1500,
                    help="grey-area events sent to Jev for topic classification per run (rest deferred)")
    ap.add_argument("--threshold", type=float, default=relations.RELATION_THRESHOLD,
                    help="Jev confidence required on the relation itself (applied to cached answers too)")
    ap.add_argument("--gate-threshold", type=float, default=relations.GATE_THRESHOLD,
                    help="Jev confidence required that both markets measure the same underlying thing")
    ap.add_argument("--implication-gate-threshold", type=float, default=relations.IMPLICATION_GATE_THRESHOLD,
                    help="the same, when every confident relation is an implication (If A then B / If B then A)")
    ap.add_argument("--no-polymarket", action="store_true")
    ap.add_argument("--paper", action="store_true", help="paper-trade every tradeable arb (all legs or none)")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    events = fetch_kalshi_events(args.horizon_days)
    print(f"Kalshi: {len(events)} candidate events settling within {args.horizon_days:g} days")
    if not args.no_polymarket:
        try:
            pm_events = fetch_polymarket_events(args.horizon_days)
            print(f"Polymarket: {len(pm_events)} candidate Yes/No events settling within {args.horizon_days:g} days")
            events += pm_events
        except Exception as exc:  # noqa: BLE001
            print(f"Polymarket: failed ({exc}), continuing with Kalshi only")

    # Cultural / economic scope: rules for clear categories, Jev for grey ones.
    classifier = TopicClassifier(max_new=args.max_new_topics, workers=args.workers)
    topics = classifier.classify(topic_subjects(events))
    print(f"topics: {classifier.stats['rule']} by rule, {classifier.stats['cached']} cached Jev verdicts, "
          f"{classifier.stats['asked']} newly judged by Jev, {classifier.stats['deferred']} deferred to a later run")
    contracts = kalshi_contracts(events, topics) + polymarket_contracts(events, topics)
    universe: dict[str, dict[str, int]] = {}
    for c in contracts:
        universe.setdefault(c.venue, {}).setdefault(c.topic, 0)
        universe[c.venue][c.topic] += 1
    print(f"in scope: {len(contracts)} markets {universe}")

    pairs = relations.candidate_pairs(contracts, max_pairs=args.max_pairs)
    print(f"{len(pairs)} candidate pair(s) by shared title words")

    # Families the rules read exactly (structural.py): their verdicts replace
    # Jev's, and they bring pairs the title-word matcher's per-event cap drops.
    rule_verdicts: dict[str, structural.Verdict] = {}
    for a, b, _ in pairs:
        v = structural.judge(a, b)
        if v:
            rule_verdicts[relations.pair_key(a, b)] = v
    seen = {relations.pair_key(a, b) for a, b, _ in pairs}
    for a, b in structural.family_pairs(contracts):
        k = relations.pair_key(a, b)
        if k not in seen:
            seen.add(k)
            pairs.append((a, b, 0.0))
            rule_verdicts[k] = structural.judge(a, b)
    by_family: dict[str, int] = {}
    for v in rule_verdicts.values():
        by_family[v.family] = by_family.get(v.family, 0) + bool(v.relations)
    print(f"{len(rule_verdicts)} pair(s) judged by family rules ({by_family} with a relation); the rest go to Jev")

    cache = classify_pairs([p for p in pairs if relations.pair_key(p[0], p[1]) not in rule_verdicts],
                           load_cache(), args.max_new_pairs, args.workers)

    opportunities: list[tuple[relations.Arb, dict | None]] = []
    comparisons: list[dict] = []
    watch: list[tuple[relations.Contract, relations.Contract, list[str], dict]] = []
    confirmed = near_miss = inconsistent = 0
    for a, b, _score in pairs:
        key = relations.pair_key(a, b)
        rule = rule_verdicts.get(key)
        if rule:
            rels = rule.relations
            why_not = None if rels else f"rule ({rule.family}): {rule.note}"
            verdict = {"probs": {**{r: float(r in rels) for r in relations.RELATIONS}, "same_underlying": 1.0},
                       "route": "rules", "family": rule.family,
                       "asked_at": (cache.get(key) or {}).get("asked_at")}
        else:
            verdict = cache.get(key)
            if not verdict or verdict.get("route") != "typesafe":
                continue
            rels, why_not = verdict_relations(verdict, args.threshold, args.gate_threshold, args.implication_gate_threshold)
            rels, vetoed = structural.restrict(a, b, rels)
            why_not = vetoed or why_not
            verdict = {**verdict, "family": "jev"}
        if why_not and why_not.startswith("inconsistent"):
            inconsistent += 1
        row, arbs = comparison_row(a, b, verdict, rels, why_not)
        comparisons.append(row)
        if not rels:
            if max(verdict["probs"][r] for r in relations.RELATIONS) >= 0.7:
                near_miss += 1
            continue
        confirmed += 1
        watch.append((a, b, rels, verdict))
        print(f"  RELATION {rels}: {a.ticker} <-> {b.ticker}")
        opportunities += [(arb, verdict) for arb in arbs if arb.edge_per_set > 0]
    write_comparisons(comparisons, universe, total_judged=len(cache))
    # Whole events priced as one set: NO on every market of a winner-take-all
    # event (Kalshi mutually_exclusive / Polymarket negRisk), and YES on every
    # bracket of a set that provably covers all prices (structural.covers_all).
    by_event: dict[str, list[relations.Contract]] = {}
    for c in contracts:
        by_event.setdefault(c.event_id, []).append(c)
    groups = []
    for eid, members in by_event.items():
        if len(members) >= 2 and all(c.event_mutually_exclusive for c in members):
            groups.append({"kind": "me_event", "event": eid, "contracts": members})
        if len(members) >= 2 and structural.covers_all(members):
            groups.append({"kind": "partition", "event": eid, "contracts": members})
    print(f"{len(groups)} event group(s) priced as sets "
          f"({sum(g['kind'] == 'partition' for g in groups)} complete bracket sets)")
    # The fast loop (run_relation_watcher.py) re-prices exactly these pairs and groups live.
    write_watchlist(watch, groups=groups)
    for g in groups:
        arb = price_group(g)
        if arb is not None and arb.edge_per_set > 0:
            opportunities.append((arb, {"family": f"group:{g['kind']}"}))

    rows = []
    opportunities.sort(key=lambda o: o[0].edge_per_set, reverse=True)
    # The watcher trades from the same ledger every few seconds: hold the
    # lock from reading the book to the last buy so neither double-buys.
    with ledger.ledger_lock() if args.paper else contextlib.nullcontext():
        broker, held = None, HeldBook()
        if args.paper:
            broker = PaperBroker.from_ledger(settled=settled_payouts())
            held = HeldBook.from_broker(broker)
            print(f"[book] cash ${broker.cash_usd:.2f}, {len(held.side_of)} open position(s)")
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
    append_arb_rows(rows)

    tradeable = sum(1 for arb, _ in opportunities if arb.tradeable)
    print(f"\n{confirmed} pair(s) with a Jev-confirmed relation (>= {args.threshold}); {near_miss} near miss(es) "
          f"(a relation >= 0.70 that didn't clear both bars); {inconsistent} rejected as inconsistent")
    print(f"{len(opportunities)} priced opportunit(ies) with positive edge, {tradeable} tradeable; logged to {ARBS_PATH}")
    if broker is not None:
        print(f"paper cash remaining: ${broker.cash_usd:.2f}")


if __name__ == "__main__":
    main()
