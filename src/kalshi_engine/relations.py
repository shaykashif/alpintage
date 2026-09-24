"""Relationship arbitrage across short-dated event markets (Kalshi and
Polymarket): Jev decides how two markets are LOGICALLY related, then plain
arithmetic decides whether the prices violate that relation.

Four relations, each with a two-leg position that pays at least $1 in
every outcome the relation allows -- so if the relation is right, a set
costing under $1 after fees is a true arbitrage, not a forecast:

    relation               meaning                      buy            pays >= $1 because
    a_implies_b            A YES  => B YES              NO A + YES B   A YES forces B YES; A NO pays the NO
    b_implies_a            B YES  => A YES              YES A + NO B   (mirror)
    mutually_exclusive     not both YES                 NO A + NO B    at least one NO pays
    exhaustive             at least one YES             YES A + YES B  at least one YES pays

Plus one check that needs no Jev at all: a Kalshi event flagged
`mutually_exclusive` where buying NO on every market costs less than the
guaranteed (n - 1) payout (me_event_arb).

The whole risk sits in the relation being right under BOTH rulebooks
(settlement source, timing window, edge cases) -- the same failure mode as
the Sep 23 bracket loss, where a set assumed complete wasn't. Hence:
Jev reads the full rules and answers the four relation questions plus a
"same underlying?" gate, for the pair in BOTH orders (lower answer wins);
the gate and the relation must each clear their bar, and the answers must
be internally consistent (see classify) before anything trades. A mock
Jev answer never trades.

No I/O here: fetching lives in relation_sources.py, orchestration in
scripts/run_relation_scanner.py.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .fees import taker_fee

# Two bars, both applied to the LOWER of the answers from asking a pair in
# both orders (merge_orders). Calibrated on 17 hand-labeled live pairs
# (2026-09-24): the same_underlying gate split true from false cleanly
# (false 0.03-0.04, true 0.88-0.96), while the relation probability alone
# did not (a false cross-tenor implication scored 0.87 asked one way, the
# same as true ones). At these bars: 7 true accepted, 0 false accepted, 6
# true missed. 17 pairs is a small sample -- nothing downstream double-
# checks a relation (no human review, by the project owner's choice), so
# these err strict; re-calibrate as data/relations_cache.jsonl grows.
RELATION_THRESHOLD = 0.80
GATE_THRESHOLD = 0.90
# Lower gate for pure implications (A => B, B => A, or both), by the
# project owner's choice (2026-09-24). Still far above the false pairs'
# 0.03-0.04 in the calibration set, and admits true ones that scored
# 0.88-0.89. "Not both" / "at least one" keep GATE_THRESHOLD.
IMPLICATION_GATE_THRESHOLD = 0.80
IMPLICATIONS = frozenset({"a_implies_b", "b_implies_a"})

# Minimum guaranteed profit per $1 set, net of fees, worth taking.
MIN_EDGE = 0.005

# A "guaranteed" edge this large almost always means the relation is wrong
# (misread rules, different settlement dates), not free money. Logged, never
# traded -- the same lesson as MAX_PLAUSIBLE_EDGE in the sports model.
MAX_PLAUSIBLE_EDGE = 0.25

RELATIONS = ("a_implies_b", "b_implies_a", "mutually_exclusive", "exhaustive")

_COMMON = (
    " Judge ONLY from the full resolution rules of BOTH markets: the exact "
    "event definition and thresholds, the settlement source each uses, the "
    "measurement date or time window, time zones, and how edge cases resolve "
    "(cancellation, postponement, ties, data revisions, early close, a market "
    "voiding). Answer YES only if it holds in EVERY possible scenario under "
    "both rulebooks as written -- not just the typical or likely one. If the "
    "two markets could settle from different sources or on different dates in "
    "a way that breaks it, answer NO. Ignore prices."
)

RELATION_QUESTIONS = {
    "a_implies_b": "Is it logically guaranteed that if market A resolves YES, market B also resolves YES?" + _COMMON,
    "b_implies_a": "Is it logically guaranteed that if market B resolves YES, market A also resolves YES?" + _COMMON,
    "mutually_exclusive": "Is it logically guaranteed that markets A and B can NOT both resolve YES?" + _COMMON,
    "exhaustive": "Is it logically guaranteed that at least one of markets A and B resolves YES?" + _COMMON,
    # Gate. Seen live: Jev gave 0.87 to "7Y yield above X at month end
    # implies 10Y monthly high above X" -- a different bond, so false -- the
    # same score it gave true same-tenor implications. Asking directly
    # whether both markets measure the same thing separates those cases.
    "same_underlying": (
        "Do markets A and B resolve on the SAME underlying thing -- the same "
        "security or index and the same tenor/maturity, the same chart or "
        "ranking list for the same period, the same person, entity, or "
        "statistic -- read from the same or equivalent official source? "
        "Different positions or thresholds on the SAME list or series count "
        "as the same underlying (e.g. #1 and #2 on the same weekly chart, or "
        "two strike levels of the same yield). Answer NO if they concern "
        "different instruments (e.g. a 7-year versus a 10-year Treasury), "
        "different lists or regions, different people, or different measures, "
        "even if they are closely related or usually move together."
    ),
}

# Each relation's name when A and B are swapped -- a pair is asked in both
# orders and a relation only counts if it holds in both (see merge_orders).
SWAPPED = {
    "a_implies_b": "b_implies_a",
    "b_implies_a": "a_implies_b",
    "mutually_exclusive": "mutually_exclusive",
    "exhaustive": "exhaustive",
    "same_underlying": "same_underlying",
}


def merge_orders(probs_ab: dict[str, float], probs_ba: dict[str, float]) -> dict[str, float]:
    """Combine answers from asking (A, B) and (B, A): each relation keeps
    the LOWER of its two probabilities, mapped back to A/B roles. An
    answer that flips with presentation order isn't a logical certainty."""
    return {name: min(probs_ab.get(name, 0.0), probs_ba.get(SWAPPED[name], 0.0)) for name in SWAPPED}

# Relation sets that make sense together. Anything else (e.g. "A implies B"
# AND "mutually exclusive", which would mean A can never happen) is a sign
# Jev misread something -- skip the pair rather than trade a contradiction.
_CONSISTENT = {
    frozenset({"a_implies_b"}),
    frozenset({"b_implies_a"}),
    frozenset({"a_implies_b", "b_implies_a"}),  # equivalent
    frozenset({"mutually_exclusive"}),
    frozenset({"exhaustive"}),
    frozenset({"mutually_exclusive", "exhaustive"}),  # complements
}

# (side on A, side on B) for each relation's arbitrage set.
_LEGS = {
    "a_implies_b": ("no", "yes"),
    "b_implies_a": ("yes", "no"),
    "mutually_exclusive": ("no", "no"),
    "exhaustive": ("yes", "yes"),
}


@dataclass
class Contract:
    """One binary market, venue-neutral. `context` is everything Jev reads
    (rules, sources, dates) and deliberately contains no prices: the
    relation is a question of logic, and a price in view invites Jev to
    judge the trade instead of the rulebooks."""
    venue: str  # "kalshi" | "polymarket"
    ticker: str  # Kalshi ticker, or "PM-<slug>"
    event_id: str
    category: str | None
    title: str
    context: str
    close_time: str | None
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None = None  # contracts resting at the best bid
    yes_ask_size: float | None = None
    event_mutually_exclusive: bool = False
    fee_rate: float = 0.0  # Polymarket taker rate; unused for Kalshi
    fee_exponent: float = 1.0
    topic: str | None = None  # "cultural" | "economic" | "geopolitical" (topics.py)

    def ask(self, side: str) -> float | None:
        """Price to BUY `side`. A NO ask is the complement of the YES bid."""
        if side == "yes":
            return self.yes_ask if self.yes_ask is not None and 0 < self.yes_ask < 1 else None
        if self.yes_bid is None or not 0 < self.yes_bid < 1:
            return None
        return round(1 - self.yes_bid, 4)

    def size(self, side: str) -> float | None:
        """Contracts available at that ask (None = venue doesn't say)."""
        return self.yes_ask_size if side == "yes" else self.yes_bid_size

    def fee(self, qty: float, price: float) -> float:
        if self.venue == "kalshi":
            return float(taker_fee(qty, price))
        # Polymarket's published schedule (rate, exponent, taker-only), read
        # per market. Treated as rate * qty * (p(1-p))^exponent -- verify
        # the exact formula against Polymarket's fee docs before trusting
        # a thin edge on a fee-enabled Polymarket leg.
        return round(self.fee_rate * qty * (price * (1 - price)) ** self.fee_exponent, 4)

    @property
    def context_hash(self) -> str:
        return hashlib.sha1(self.context.encode("utf-8")).hexdigest()[:12]


@dataclass
class ArbLeg:
    contract: Contract
    side: str
    price: float


@dataclass
class Arb:
    kind: str  # a relation name, or "me_event_overround"
    legs: list[ArbLeg]
    qty: int
    payout_per_set: float  # guaranteed minimum payout per set of legs
    cost_per_set: float  # sum of leg prices + fees per set
    edge_per_set: float  # payout - cost
    fees_usd: float  # total fees at qty
    reason: str | None = None  # why NOT tradeable, when set
    meta: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.reason is None


# --- Jev plumbing ---------------------------------------------------------

def relation_state(a: Contract, b: Contract, today: str | None = None) -> str:
    today = today or datetime.now(timezone.utc).date().isoformat()
    return f"Today is {today}.\n\nMARKET A\n{a.context}\n\nMARKET B\n{b.context}"


def classify(probs: dict[str, float], threshold: float = RELATION_THRESHOLD,
             gate_threshold: float = GATE_THRESHOLD,
             implication_gate_threshold: float = IMPLICATION_GATE_THRESHOLD) -> tuple[list[str], str | None]:
    """Relations Jev is confident in, or ([], why not). Requires the
    same-underlying gate to clear its bar (the lower implication bar when
    every confident relation is an implication), and rejects relation sets
    that contradict each other."""
    confident = frozenset(r for r in RELATIONS if probs.get(r, 0.0) >= threshold)
    if not confident:
        return [], None
    gate = implication_gate_threshold if confident <= IMPLICATIONS else gate_threshold
    if probs.get("same_underlying", 0.0) < gate:
        return [], f"same_underlying {probs.get('same_underlying', 0.0):.2f} below threshold"
    if confident not in _CONSISTENT:
        return [], f"inconsistent relation set {sorted(confident)}"
    return sorted(confident), None


# --- Structural veto: ranked lists -------------------------------------------
# Seen live (2026-09-24): Jev called "Moonshot is the THIRD-best Chinese AI
# company" and "Zhipu is the SECOND-best" mutually exclusive, and cleared
# the same-underlying gate (same list). Different positions on one list can
# both be YES -- Zhipu #2 and Moonshot #3 -- so "not both" (and "at least
# one") only holds between markets about the SAME position. Checked here in
# code, since Jev got it wrong with confidence; caught that time only by the
# implausible-edge cap.

_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_ORD = r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d{1,3}(?:st|nd|rd|th))"
# An ordinal only counts as a rank next to ranking words, so "first quarter"
# or "second round" never do.
_RANK_PATTERNS = [
    re.compile(_ORD + r"[\s-]+(?:best|largest|biggest|most|highest|lowest|top|ranked|place|strongest|richest|popular)\b"),
    re.compile(r"(?:#|\bno\.\s*|\bnumber\s+|\branked\s+|\brank\s+#?)(\d{1,3})\b"),
    re.compile(r"\bin\s+" + _ORD + r"\s+place\b"),
]
_RANK_LINES = ("Event:", "Market:", "Question:", "YES means:")


def _rank_value(tok: str) -> int:
    return _ORDINAL_WORDS.get(tok) or int(re.match(r"\d+", tok).group())


def list_ranks(c: Contract) -> set[int]:
    """List positions a market's headline names ("second-best", "#2",
    "No. 2", "2nd place"). Reads the title and the headline lines of its
    context -- never the rules, which mention other positions for ties."""
    lines = [c.title] + [l for l in c.context.splitlines() if l.startswith(_RANK_LINES)]
    text = " ".join(lines).lower()
    return {_rank_value(m.group(1)) for p in _RANK_PATTERNS for m in p.finditer(text)}


def structural_veto(rels: list[str], a: Contract, b: Contract) -> str | None:
    """Why a Jev-confirmed relation set must be dropped regardless of its
    probabilities, or None. Currently: "not both" / "at least one" between
    markets about different positions on a ranked list."""
    if not {"mutually_exclusive", "exhaustive"} & set(rels):
        return None
    ra, rb = list_ranks(a), list_ranks(b)
    if len(ra) == 1 and len(rb) == 1 and ra != rb:
        return f"different list positions (#{min(ra)} vs #{min(rb)}) can both resolve YES"
    return None


def pair_key(a: Contract, b: Contract) -> str:
    """Stable cache key: changes if either market's rules text changes."""
    return f"{a.ticker}|{a.context_hash}||{b.ticker}|{b.context_hash}"


# --- Arithmetic -----------------------------------------------------------

def _size_qty(legs: list[ArbLeg], max_notional_per_leg: float, max_qty: int) -> int:
    qty = max_qty
    for leg in legs:
        qty = min(qty, math.floor(max_notional_per_leg / leg.price))
        size = leg.contract.size(leg.side)
        if size is not None:
            qty = min(qty, math.floor(size))  # never assume depth that isn't there
    return max(qty, 0)


def _evaluate(kind: str, legs: list[ArbLeg], payout: float, max_notional_per_leg: float, max_qty: int) -> Arb:
    qty = _size_qty(legs, max_notional_per_leg, max_qty)
    if qty < 1:
        # Still report the edge a 1-lot would have AFTER fees, so an
        # untradeable row never looks better than it is.
        fees1 = sum(l.contract.fee(1, l.price) for l in legs)
        cost1 = sum(l.price for l in legs) + fees1
        return Arb(kind, legs, 0, payout, round(cost1, 4), round(payout - cost1, 4), round(fees1, 4),
                   reason="no depth at the quoted prices")
    fees = sum(l.contract.fee(qty, l.price) for l in legs)
    cost = sum(l.price for l in legs) + fees / qty
    edge = payout - cost
    arb = Arb(kind, legs, qty, payout, round(cost, 4), round(edge, 4), round(fees, 4))
    if edge <= MIN_EDGE:
        arb.reason = f"edge {edge:.4f} <= MIN_EDGE"
    elif edge / payout > MAX_PLAUSIBLE_EDGE:
        arb.reason = f"edge {edge:.3f} implausibly large -- relation probably wrong"
    return arb


def relation_arb(relation: str, a: Contract, b: Contract, max_notional_per_leg: float = 10.0, max_qty: int = 500) -> Arb | None:
    """The two-leg arbitrage implied by `relation`, priced at the current
    asks. None if a leg has no ask at all."""
    side_a, side_b = _LEGS[relation]
    pa, pb = a.ask(side_a), b.ask(side_b)
    if pa is None or pb is None:
        return None
    legs = [ArbLeg(a, side_a, pa), ArbLeg(b, side_b, pb)]
    return _evaluate(relation, legs, 1.0, max_notional_per_leg, max_qty)


def missing_quotes(relation: str, a: Contract, b: Contract) -> list[str]:
    """Why relation_arb() returned None: which leg can't be bought. Buying
    YES needs someone selling YES (an ask below $1); buying NO needs
    someone bidding YES (a bid above $0). Seen live on nearly-decided
    strikes, where one side of the book is simply empty."""
    out = []
    for c, side, label in ((a, _LEGS[relation][0], "A"), (b, _LEGS[relation][1], "B")):
        if c.ask(side) is None:
            out.append(f"no seller of YES on {label}" if side == "yes" else f"no YES bid on {label} (so no NO to buy)")
    return out


def me_event_arb(contracts: list[Contract], max_notional_per_leg: float = 10.0, max_qty: int = 500) -> Arb | None:
    """Kalshi says at most one market in this event resolves YES, so NO on
    all n of them pays at least n - 1. If those NOs cost less (after fees),
    that's an arbitrage -- no Jev needed, the exchange asserted the
    relation. Every market in the event must be present and quoted."""
    if len(contracts) < 2 or not all(c.event_mutually_exclusive for c in contracts):
        return None
    legs = []
    for c in contracts:
        p = c.ask("no")
        if p is None:
            return None
        legs.append(ArbLeg(c, "no", p))
    return _evaluate("me_event_overround", legs, float(len(contracts) - 1), max_notional_per_leg, max_qty)


# --- Candidate pairs --------------------------------------------------------

_WORD = re.compile(r"[a-z][a-z']+|\d+(?:\.\d+)?")
_STOP = {
    "the", "and", "for", "will", "what", "who", "when", "which", "this", "that", "with", "from",
    "above", "below", "between", "more", "less", "than", "least", "most", "over", "under",
    "yes", "not", "any", "how", "much", "many", "day", "week", "month", "year", "end", "top",
    "high", "low", "price", "market", "resolve", "resolves", "before", "after", "during", "win",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "2025", "2026", "2027", "or", "of", "in", "on", "at", "by", "be",
}


def _tokens(c: Contract) -> set[str]:
    return {t for t in _WORD.findall(c.title.lower()) if t not in _STOP and (len(t) > 2 or t[0].isdigit())}


def candidate_pairs(contracts: list[Contract], max_pairs: int = 2000, max_df: int = 150,
                    min_shared: int = 2, min_score: float = 0.35,
                    max_per_event_pair: int = 3) -> list[tuple[Contract, Contract, float]]:
    """Pairs worth asking Jev about, best first. Two markets are candidates
    if their titles share at least `min_shared` distinctive tokens (rare
    words count more -- IDF weighting) and they aren't in the same event
    (within-event relations are the ladder/bracket/ME checks' job). Tokens
    so common they'd pair half the catalog (df > max_df) are ignored.
    Each pair comes back ordered (lower ticker first) so its cache key and
    A/B roles are stable across runs.

    At most `max_per_event_pair` pairs per (event, event) combination:
    threshold ladders (e.g. two 20-strike Treasury-yield events) otherwise
    produce hundreds of near-identical pairs that eat the whole Jev budget
    (seen live on the first run) before any other event combination is
    looked at."""
    n = len(contracts)
    toks = [_tokens(c) for c in contracts]
    df: dict[str, int] = {}
    for ts in toks:
        for t in ts:
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(n / d) for t, d in df.items() if d <= max_df and d >= 1}

    postings: dict[str, list[int]] = {}
    for i, ts in enumerate(toks):
        for t in ts:
            if t in idf:
                postings.setdefault(t, []).append(i)

    shared: dict[tuple[int, int], list[str]] = {}
    for t, idxs in postings.items():
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]
                if contracts[i].event_id == contracts[j].event_id:
                    continue
                shared.setdefault((i, j), []).append(t)

    norm = [math.sqrt(sum(idf.get(t, 0.0) ** 2 for t in ts)) or 1.0 for ts in toks]
    scored = []
    for (i, j), ts in shared.items():
        if len(ts) < min_shared:
            continue
        score = sum(idf[t] ** 2 for t in ts) / (norm[i] * norm[j])
        if score >= min_score:
            a, b = sorted((contracts[i], contracts[j]), key=lambda c: c.ticker)
            scored.append((a, b, round(score, 4)))
    scored.sort(key=lambda s: s[2], reverse=True)
    per_combo: dict[tuple[str, str], int] = {}
    out = []
    for a, b, score in scored:
        combo = (a.event_id, b.event_id)
        if per_combo.get(combo, 0) >= max_per_event_pair:
            continue
        per_combo[combo] = per_combo.get(combo, 0) + 1
        out.append((a, b, score))
        if len(out) >= max_pairs:
            break
    return out
