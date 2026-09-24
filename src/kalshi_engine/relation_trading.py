"""Shared by the two relation-arb loops:

- scripts/run_relation_scanner.py -- slow discovery (every loop cycle):
  fetches every in-scope market, has Jev judge candidate pairs, prices
  them, and writes the WATCHLIST of every confirmed relation.
- scripts/run_relation_watcher.py -- fast pricing (every few seconds):
  re-quotes only the watchlisted markets, re-prices every relation, and
  paper-trades a violation the moment it appears.

Both trade through execute(), under ledger.ledger_lock, so the two can
never buy the same violation twice.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from . import relations
from .paper_broker import PaperBroker

DATA = Path(__file__).resolve().parents[2] / "data"
ARBS_PATH = DATA / "relation_arbs.jsonl"
WATCHLIST_PATH = DATA / "relation_watchlist.json"
LIVE_PATH = DATA / "relation_live.json"
SUMMARY_PATH = DATA / "paper_pnl_summary.json"

STRATEGY = "relation_arb"


def write_json_atomic(path: Path, obj) -> None:
    """Readers (the dashboard, the watcher) poll these files every few
    seconds; a half-written file would read as garbage, so write-then-rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def pair_id(a: relations.Contract, b: relations.Contract) -> str:
    """Joins a watchlist pair to its row in relation_comparisons.json."""
    return f"{a.ticker}|{b.ticker}"


# ---- Pricing -------------------------------------------------------------------

def price_relations(a: relations.Contract, b: relations.Contract, rels: list[str]) -> dict:
    """Status of a confirmed relation at the contracts' current quotes:
    violation (best set pays more than it costs), consistent, or unpriced
    (a leg has nothing to buy). `arbs` holds every priced set."""
    arbs = [x for x in (relations.relation_arb(rel, a, b) for rel in rels) if x is not None]
    if not arbs:
        gaps = sorted({g for rel in rels for g in relations.missing_quotes(rel, a, b)})
        return {"status": "unpriced", "edge": None, "note": "; ".join(gaps) or None, "arbs": []}
    edge = max(x.edge_per_set for x in arbs)
    return {"status": "violation" if edge > 0 else "consistent", "edge": edge, "note": None, "arbs": arbs}


# ---- Watchlist -------------------------------------------------------------------

_CONTRACT_FIELDS = {f.name for f in fields(relations.Contract)}


def _contract_dict(c: relations.Contract) -> dict:
    d = asdict(c)
    d["context"] = ""  # full rulebooks: only Jev reads them, and the watcher never asks Jev
    return d


def write_watchlist(entries: list[tuple[relations.Contract, relations.Contract, list[str], dict]],
                    path: Path = WATCHLIST_PATH) -> None:
    """Every Jev-confirmed pair from the latest scan: (a, b, relations, verdict)."""
    write_json_atomic(path, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs": [{"a": _contract_dict(a), "b": _contract_dict(b), "relations": rels, "probs": v.get("probs")}
                  for a, b, rels, v in entries],
    })


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict]:
    """[{"a": Contract, "b": Contract, "relations": [...], "verdict": {...}}].
    Markets appearing in several pairs share one Contract object, so one
    quote update reaches every pair."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    shared: dict[str, relations.Contract] = {}

    def contract(d: dict) -> relations.Contract:
        c = shared.get(d["ticker"])
        if c is None:
            c = shared[d["ticker"]] = relations.Contract(**{k: v for k, v in d.items() if k in _CONTRACT_FIELDS})
        return c

    return [{"a": contract(p["a"]), "b": contract(p["b"]), "relations": p["relations"],
             "verdict": {"probs": p.get("probs")}} for p in body.get("pairs", [])]


# ---- Trading -------------------------------------------------------------------

def arb_row(arb: relations.Arb, verdict: dict | None, source: str = "scan") -> dict:
    return {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "source": source,  # "scan" (discovery loop) or "watch" (fast pricing loop)
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


def append_arb_rows(rows: list[dict], path: Path = ARBS_PATH) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def settled_payouts(path: Path = SUMMARY_PATH) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {p["ticker"]: p.get("payout", 0.0) for p in summary.get("positions", []) if p.get("status") == "settled"}


def execute(arb: relations.Arb, broker: PaperBroker, held: set[str], verdict: dict | None) -> tuple[bool, str]:
    """Buy every leg or none. A half-filled arb is a directional bet -- the
    exact failure behind the Sep 23 bracket losses -- so every check that
    could veto a leg runs BEFORE the first buy. Callers hold
    ledger.ledger_lock and built `broker` inside it."""
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
            payout_per_set=arb.payout_per_set,
            edge=arb.edge_per_set, jev_probs=verdict["probs"] if verdict else None,
        )
        held.add(leg.contract.ticker)
    return True, "filled"
