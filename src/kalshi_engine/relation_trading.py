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
import math
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, relations
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
                    path: Path = WATCHLIST_PATH, groups: list[dict] | None = None) -> None:
    """Every confirmed pair from the latest scan: (a, b, relations, verdict),
    plus `groups` -- whole events priced as one set: {"kind": "me_event" |
    "partition", "event": id, "contracts": [Contract]} (see price_group)."""
    write_json_atomic(path, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs": [{"a": _contract_dict(a), "b": _contract_dict(b), "relations": rels, "probs": v.get("probs"),
                   "family": v.get("family")} for a, b, rels, v in entries],
        "groups": [{"kind": g["kind"], "event": g["event"], "contracts": [_contract_dict(c) for c in g["contracts"]]}
                   for g in groups or []],
    })


def price_group(g: dict) -> relations.Arb | None:
    """me_event: NO on every quoted market of a winner-take-all event (pays
    n - 1). partition: YES on every bracket of a set that covers all prices
    (pays $1)."""
    if g["kind"] == "me_event":
        return relations.me_event_arb(g["contracts"])
    return relations.partition_arb(g["contracts"])


def load_watchlist(path: Path = WATCHLIST_PATH, with_groups: bool = False):
    """[{"a": Contract, "b": Contract, "relations": [...], "verdict": {...}}]
    -- or with `with_groups`, (pairs, groups). Markets appearing in several
    pairs or groups share one Contract object, so one quote update reaches
    every one of them."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ([], []) if with_groups else []
    shared: dict[str, relations.Contract] = {}

    def contract(d: dict) -> relations.Contract:
        c = shared.get(d["ticker"])
        if c is None:
            c = shared[d["ticker"]] = relations.Contract(**{k: v for k, v in d.items() if k in _CONTRACT_FIELDS})
        return c

    pairs = [{"a": contract(p["a"]), "b": contract(p["b"]), "relations": p["relations"],
              "verdict": {"probs": p.get("probs"), "family": p.get("family")}} for p in body.get("pairs", [])]
    if not with_groups:
        return pairs
    groups = [{"kind": g["kind"], "event": g["event"], "contracts": [contract(d) for d in g["contracts"]]}
              for g in body.get("groups", [])]
    return pairs, groups


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
        "jev_probs": verdict.get("probs") if verdict else None,
        # Which judged the relation: a structural.py family, "jev", or None (Kalshi ME-event check).
        "family": verdict.get("family") if verdict else None,
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
    # Summed per ticker: the broker holds one position per market, which may
    # be scored as several sets.
    out: dict[str, float] = {}
    for p in summary.get("positions", []):
        if p.get("status") == "settled":
            out[p["ticker"]] = out.get(p["ticker"], 0.0) + p.get("payout", 0.0)
    return out


def arb_group(arb: relations.Arb) -> str:
    """One id per set: its relation and legs. Top-ups of a set share it."""
    return f"{arb.kind}:{'|'.join(l.contract.ticker for l in arb.legs)}"


@dataclass
class HeldBook:
    """Which sets each held market is a leg of, the side it's held on, and
    the edge each set was last entered at -- what execute() needs to tell a
    top-up of a held set from a new set, and to refuse a new set that would
    hold a market on the opposite side."""
    groups_of: dict[str, set[str]] = field(default_factory=dict)  # held ticker -> its sets' arb_groups
    side_of: dict[str, str] = field(default_factory=dict)  # held ticker -> "yes" | "no"
    last_edge: dict[str, float] = field(default_factory=dict)  # arb_group -> edge/set at its latest entry

    @classmethod
    def from_broker(cls, broker: PaperBroker) -> HeldBook:
        """Only markets the broker still holds (it already dropped settled ones)."""
        rows = ledger.load_rows(broker.log_path)
        book = cls(side_of={t: p["side"] for t, p in broker.positions.items()})
        for p in ledger.build_positions(rows, by_set=True).values():
            g = p.meta.get("arb_group")
            if p.ticker in broker.positions and p.qty_open > 0 and g:
                book.groups_of.setdefault(p.ticker, set()).add(g)
        for t in broker.positions:
            book.groups_of.setdefault(t, set())  # held outside any set (another strategy)
        for r in rows:
            if r.get("event") == "fill" and r.get("arb_group") and r.get("edge") is not None:
                book.last_edge[r["arb_group"]] = r["edge"]
        return book

    def add(self, ticker: str, side: str, group: str) -> None:
        self.groups_of.setdefault(ticker, set()).add(group)
        self.side_of[ticker] = side


# A top-up must beat the set's last entry by this much per set: re-buying
# at the same quote would count the same resting depth twice.
TOPUP_MIN_IMPROVEMENT = 0.005


def execute(arb: relations.Arb, broker: PaperBroker, held: HeldBook, verdict: dict | None) -> tuple[bool, str]:
    """Buy every leg or none. A half-filled arb is a directional bet -- the
    exact failure behind the Sep 23 bracket losses -- so every check that
    could veto a leg runs BEFORE the first buy. Callers hold
    ledger.ledger_lock and built `broker` (and `held` from it) inside it.

    A set already held can be topped up -- more of the same legs, when the
    new edge beats the last entry's by TOPUP_MIN_IMPROVEMENT -- since every
    extra set pays out on its own. A new set may share a market with other
    held sets (each set is hedged on its own; the scorer and exits track
    them per set), but only on the SAME side: the broker keeps one position
    per market, which can't be long YES and NO at once."""
    tickers = [l.contract.ticker for l in arb.legs]
    group = arb_group(arb)
    for leg in arb.legs:
        held_side = held.side_of.get(leg.contract.ticker)
        if held_side is not None and held_side != leg.side:
            return False, f"{leg.contract.ticker} is held on the other side"
    topup = all(group in held.groups_of.get(t, set()) for t in tickers)
    if topup:
        last = held.last_edge.get(group)
        if last is not None and arb.edge_per_set < last + TOPUP_MIN_IMPROVEMENT:
            return False, f"already hold this set (entered at {last:.3f}/set)"
    # Per-market room (max_position_usd): shrink the set to fit rather than half-fill it.
    position_usd = broker._position_usd_by_ticker()
    room = min(math.floor((broker.limits.max_position_usd - position_usd.get(l.contract.ticker, 0.0)) / l.price)
               for l in arb.legs)
    if room < arb.qty:
        arb = relations.resize(arb, room)
        if arb.qty < 1:
            return False, "per-market position limit reached"
        if not arb.tradeable:
            return False, f"after resizing to the per-market limit: {arb.reason}"
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

    for leg in arb.legs:
        fee = None if leg.contract.venue == "kalshi" else leg.contract.fee(arb.qty, leg.price)
        broker.buy(
            leg.contract.ticker, leg.side, leg.price, qty=arb.qty, fee_usd=fee,
            reason=f"relation arb ({arb.kind}){' top-up' if topup else ''}, edge={arb.edge_per_set:.3f}/set",
            strategy=STRATEGY, venue=leg.contract.venue, relation=arb.kind, arb_group=group,
            payout_per_set=arb.payout_per_set, topup=topup,
            edge=arb.edge_per_set, jev_probs=verdict.get("probs") if verdict else None,
        )
        held.add(leg.contract.ticker, leg.side, group)
    held.last_edge[group] = arb.edge_per_set
    return True, f"topped up x{arb.qty}" if topup else "filled"


# ---- Early exit ------------------------------------------------------------------

# Close a held set early only when selling every leg now beats its
# guaranteed payout by at least this much per set, after exit fees.
EXIT_MIN_GAIN = relations.MIN_EDGE
_TWO_LEG_PAYOUT = {"a_implies_b": 1.0, "b_implies_a": 1.0, "mutually_exclusive": 1.0, "exhaustive": 1.0}


@dataclass
class HeldSet:
    group: str
    relation: str | None
    legs: dict[str, str]  # ticker -> side
    qty: float
    payout_per_set: float


def held_sets(rows: list[dict]) -> list[HeldSet]:
    """Every relation-arb set still held whole: all its legs open, in equal
    quantity, never partly sold. A set with a settled leg drops out at the
    watcher on its own -- a closed market has no quote to sell into."""
    positions = ledger.build_positions(rows, by_set=True)
    by_group: dict[str, list[ledger.Position]] = {}
    for p in positions.values():
        g = p.meta.get("arb_group")
        if p.strategy == STRATEGY and g and p.qty_open > 0:
            by_group.setdefault(g, []).append(p)
    out = []
    for g, legs in by_group.items():
        tickers = g.split(":", 1)[1].split("|")
        if sorted(p.ticker for p in legs) != sorted(tickers):
            continue
        if any(p.qty_sold or p.qty_open != legs[0].qty_open for p in legs):
            continue
        relation = legs[0].meta.get("relation")
        per_set = legs[0].meta.get("payout_per_set")
        if per_set is None:
            per_set = float(len(legs) - 1) if relation == "me_event_overround" else _TWO_LEG_PAYOUT.get(relation)
        if per_set is None:
            continue
        out.append(HeldSet(g, relation, {p.ticker: p.side for p in legs}, legs[0].qty_open, float(per_set)))
    return out


def exit_quote(s: HeldSet, contracts: dict[str, relations.Contract]) -> dict | None:
    """What selling the whole set now would fetch, or None if any leg can't
    be sold in full at its best bid. Exit fees use each venue's schedule."""
    legs, proceeds = [], 0.0
    for ticker, side in s.legs.items():
        c = contracts.get(ticker)
        if c is None:
            return None
        if side == "yes":
            bid, depth = c.yes_bid, c.yes_bid_size
        else:  # a NO bid is the complement of the YES ask; its depth is the YES ask's
            bid = round(1 - c.yes_ask, 4) if c.yes_ask is not None else None
            depth = c.yes_ask_size
        if not bid or bid <= 0 or depth is None or depth < s.qty:
            return None
        fee = c.fee(s.qty, bid)
        legs.append((c, side, bid, fee))
        proceeds += bid * s.qty - fee
    guaranteed = s.payout_per_set * s.qty
    return {"legs": legs, "proceeds": proceeds, "guaranteed": guaranteed,
            "gain_per_set": (proceeds - guaranteed) / s.qty}


def exit_candidates(sets: list[HeldSet], contracts: dict[str, relations.Contract]) -> list[tuple[HeldSet, dict]]:
    """Held sets whose sale value beats their guaranteed payout by EXIT_MIN_GAIN/set."""
    out = []
    for s in sets:
        q = exit_quote(s, contracts)
        if q and q["gain_per_set"] >= EXIT_MIN_GAIN:
            out.append((s, q))
    return out


def execute_exit(s: HeldSet, q: dict, broker: PaperBroker) -> tuple[bool, str]:
    """Sell every leg or none. Callers hold ledger.ledger_lock and built
    `broker` inside it; the set is re-checked against it first."""
    for ticker, side in s.legs.items():
        pos = broker.positions.get(ticker)
        if pos is None or pos["side"] != side or pos["qty"] < s.qty:
            return False, "set no longer held whole"
    if broker.limits.kill_switch_path.exists():
        return False, "kill switch active"
    for c, side, bid, fee in q["legs"]:
        broker.sell(
            c.ticker, bid, qty=s.qty, fee_usd=fee,
            reason=f"relation arb early exit: sale ${q['proceeds']:.2f} > guaranteed ${q['guaranteed']:.2f}",
            strategy=STRATEGY, venue=c.venue, relation=s.relation, arb_group=s.group,
        )
    return True, f"exited x{s.qty:g} for ${q['proceeds']:.2f} (guaranteed ${q['guaranteed']:.2f})"
