import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run_relation_scanner as rrs  # noqa: E402
from kalshi_engine import ledger, relations  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.relation_sources import polymarket_as_kalshi_shape  # noqa: E402
from kalshi_engine.relation_trading import HeldBook  # noqa: E402
from kalshi_engine.risk import RiskLimits  # noqa: E402


def C(ticker, bid, ask, event=None, title="", venue="kalshi", bid_size=1000.0, ask_size=1000.0, me=False, fee_rate=0.0):
    return relations.Contract(
        venue=venue, ticker=ticker, event_id=event or ticker, category="Entertainment",
        title=title or ticker, context=f"rules for {ticker}", close_time=None,
        yes_bid=bid, yes_ask=ask, yes_bid_size=bid_size, yes_ask_size=ask_size,
        event_mutually_exclusive=me, fee_rate=fee_rate,
    )


def _limits(tmp_path, **kw):
    d = dict(max_order_notional_usd=100.0, max_position_usd=100.0, max_total_exposure_usd=1000.0,
             max_daily_loss_usd=200.0, kill_switch_path=tmp_path / "KILL_SWITCH")
    d.update(kw)
    return RiskLimits(**d)


# --- the core guarantee ----------------------------------------------------

ALLOWED_WORLDS = {
    "a_implies_b": lambda a, b: not (a and not b),
    "b_implies_a": lambda a, b: not (b and not a),
    "mutually_exclusive": lambda a, b: not (a and b),
    "exhaustive": lambda a, b: a or b,
}


@pytest.mark.parametrize("relation", relations.RELATIONS)
def test_every_relation_set_pays_at_least_one_dollar_in_every_allowed_world(relation):
    side_a, side_b = relations._LEGS[relation]
    for a, b in itertools.product([True, False], repeat=2):
        if not ALLOWED_WORLDS[relation](a, b):
            continue
        payout = (a if side_a == "yes" else not a) + (b if side_b == "yes" else not b)
        assert payout >= 1, (relation, a, b)


# --- pricing --------------------------------------------------------------

def test_implication_violation_is_priced_as_an_arb():
    # "#1 on the Hot 100" (A) implies "Top 10" (B), yet A bids 0.60 while B
    # asks 0.50: NO A @0.40 + YES B @0.50 = 0.90 + fees for a >= $1 payout.
    a, b = C("NUMBER1", 0.60, 0.62), C("TOP10", 0.48, 0.50)
    arb = relations.relation_arb("a_implies_b", a, b)
    assert [(l.side, l.price) for l in arb.legs] == [("no", 0.40), ("yes", 0.50)]
    assert arb.tradeable and arb.edge_per_set > 0.05
    assert arb.qty == 1000  # $500 notional cap / $0.50 leg


def test_consistent_prices_are_not_an_arb():
    a, b = C("NUMBER1", 0.30, 0.32), C("TOP10", 0.70, 0.72)  # P(A) < P(B), as logic requires
    arb = relations.relation_arb("a_implies_b", a, b)
    assert arb.edge_per_set < 0 and not arb.tradeable


def test_implausibly_large_edge_is_not_tradeable():
    a, b = C("A", 0.90, 0.92), C("B", 0.10, 0.12)  # "A implies B" priced 80 points backwards
    arb = relations.relation_arb("a_implies_b", a, b)
    assert not arb.tradeable and "implausibly" in arb.reason


def test_qty_capped_by_top_of_book_depth():
    a, b = C("A", 0.60, 0.62, bid_size=3), C("B", 0.48, 0.50)
    assert relations.relation_arb("a_implies_b", a, b).qty == 3


def test_no_depth_still_reports_edge_after_fees():
    a, b = C("A", 0.60, 0.62, bid_size=0.4), C("B", 0.48, 0.50)
    arb = relations.relation_arb("a_implies_b", a, b)
    assert arb.qty == 0 and not arb.tradeable
    assert arb.edge_per_set == pytest.approx(1 - 0.40 - 0.50 - 0.02 - 0.02)  # 1-lot fees


def test_missing_quote_means_no_arb():
    assert relations.relation_arb("a_implies_b", C("A", None, 0.5), C("B", 0.4, 0.5)) is None


def test_mutually_exclusive_event_overround():
    # Kalshi says at most one is YES; YES bids sum to 1.30, so NO on all
    # three costs 0.40+0.50+0.80 = 1.70 for a guaranteed 2.00.
    legs = [C("E-1", 0.60, 0.62, event="E", me=True), C("E-2", 0.50, 0.52, event="E", me=True),
            C("E-3", 0.20, 0.22, event="E", me=True)]
    arb = relations.me_event_arb(legs)
    assert arb.payout_per_set == 2.0 and arb.tradeable
    assert arb.edge_per_set == pytest.approx(2.0 - 1.70 - arb.fees_usd / arb.qty, abs=1e-4)


def test_me_overround_needs_every_leg_quoted_and_flagged():
    legs = [C("E-1", 0.60, 0.62, event="E", me=True), C("E-2", None, 0.52, event="E", me=True)]
    assert relations.me_event_arb(legs) is None
    assert relations.me_event_arb([C("X-1", 0.6, 0.62), C("X-2", 0.6, 0.62)]) is None  # not flagged ME


def test_polymarket_fee_uses_its_schedule():
    pm = C("PM-x", 0.5, 0.5, venue="polymarket", fee_rate=0.05)
    assert pm.fee(10, 0.5) == pytest.approx(0.05 * 10 * 0.25)
    assert C("PM-y", 0.5, 0.5, venue="polymarket").fee(10, 0.5) == 0.0


# --- Jev verdict handling -------------------------------------------------

def _probs(**kw):
    base = {r: 0.05 for r in relations.SWAPPED}
    base["same_underlying"] = 0.97
    base.update(kw)
    return base


def test_classify_accepts_gated_confident_relation():
    assert relations.classify(_probs(a_implies_b=0.9)) == (["a_implies_b"], None)


def test_classify_rejects_when_gate_fails():
    # Live case: 7Y-vs-10Y "implication" scored high but they're different bonds.
    rels, why = relations.classify(_probs(b_implies_a=0.87, same_underlying=0.05))
    assert rels == [] and "same_underlying" in why


def test_implications_pass_on_same_underlying_or_on_same_source_and_subject():
    assert relations.classify(_probs(a_implies_b=0.75, same_underlying=0.96))[0] == ["a_implies_b"]
    # Pure album sales -> album-equivalent units: a different measure (same_underlying low),
    # but the same Luminate source and the same album.
    album = _probs(b_implies_a=0.8, same_underlying=0.14, same_source=0.95, same_subject=0.93)
    assert relations.classify(album)[0] == ["b_implies_a"]
    assert relations.classify({**album, "same_subject": 0.5})[0] == []  # a different album: no
    assert relations.classify(_probs(a_implies_b=0.9, same_underlying=0.9))[0] == []  # pre-v3 verdict: 0.95 bar


def test_not_both_and_at_least_one_still_need_same_underlying():
    shared = {"same_source": 0.99, "same_subject": 0.99}
    assert relations.classify({**_probs(mutually_exclusive=0.9, same_underlying=0.9), **shared})[0] == []
    assert relations.classify(_probs(exhaustive=0.9, same_underlying=0.96))[0] == ["exhaustive"]


def test_classify_rejects_contradictions():
    rels, why = relations.classify(_probs(a_implies_b=0.9, mutually_exclusive=0.9))
    assert rels == [] and "inconsistent" in why


def test_classify_equivalence_is_both_implications():
    assert relations.classify(_probs(a_implies_b=0.9, b_implies_a=0.9))[0] == ["a_implies_b", "b_implies_a"]


def test_merge_orders_maps_roles_and_keeps_the_lower_answer():
    ab = _probs(a_implies_b=0.9)
    ba = _probs(b_implies_a=0.7)  # same relation seen from the swapped order
    merged = relations.merge_orders(ab, ba)
    assert merged["a_implies_b"] == 0.7


def test_mock_verdict_never_yields_a_relation():
    verdict = {"route": "mock", "probs": _probs(a_implies_b=0.99)}
    assert rrs.verdict_relations(verdict, 0.8)[0] == []


# --- candidate pairs ------------------------------------------------------

def test_candidates_pair_related_markets_across_events_not_within():
    cs = [
        C("HOT1-GOLDEN", 0.5, 0.5, event="HOT1", title="#1 on the Billboard Hot 100 -- Golden by Huntrix"),
        C("TOP10-GOLDEN", 0.5, 0.5, event="TOP10", title="Top 10 on the Billboard Hot 100 -- Golden by Huntrix"),
        C("HOT1-OTHER", 0.5, 0.5, event="HOT1", title="#1 on the Billboard Hot 100 -- Manchild by Sabrina"),
        C("CPI-1", 0.5, 0.5, event="CPI", title="CPI above 3.1% in August"),
    ] + [C(f"FILLER-{i}", 0.5, 0.5, event=f"F{i}", title=f"unrelated market number {i} weather") for i in range(40)]
    pairs = relations.candidate_pairs(cs, min_score=0.1)
    tickers = [(a.ticker, b.ticker) for a, b, _ in pairs]
    assert ("HOT1-GOLDEN", "TOP10-GOLDEN") in tickers
    assert tickers[0] == ("HOT1-GOLDEN", "TOP10-GOLDEN")  # the song match outranks the chart-only match
    assert all(not (a.event_id == b.event_id) for a, b, _ in pairs)


def test_candidates_capped_per_event_combination():
    ladder_a = [C(f"YA-{k}", 0.5, 0.5, event="YA", title=f"30Y yield above {k} month high") for k in range(10)]
    ladder_b = [C(f"YB-{k}", 0.5, 0.5, event="YB", title=f"30Y yield above {k} month end") for k in range(10)]
    pairs = relations.candidate_pairs(ladder_a + ladder_b, min_score=0.0, min_shared=1, max_per_event_pair=3)
    assert len(pairs) == 3


# --- execution: all legs or none ------------------------------------------

def _arb():
    return relations.relation_arb("a_implies_b", C("A", 0.60, 0.62), C("PM-b", 0.48, 0.50, venue="polymarket", fee_rate=0.05))


def test_execute_buys_every_leg_with_tags_and_venue_fees(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    ok, _ = rrs.execute(_arb(), broker, HeldBook(), {"probs": {"a_implies_b": 0.9}})
    assert ok and set(broker.positions) == {"A", "PM-b"}
    rows = ledger.load_rows(tmp_path / "f.jsonl")
    assert {r["strategy"] for r in rows} == {"relation_arb"}
    pm_row = next(r for r in rows if r["ticker"] == "PM-b")
    assert pm_row["fee_usd"] == pytest.approx(0.05 * 200 * 0.5 * 0.5)  # Polymarket's fee, not Kalshi's


def test_execute_buys_nothing_if_the_whole_set_would_breach_exposure(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path, max_total_exposure_usd=12.0), log_path=tmp_path / "f.jsonl")
    ok, why = rrs.execute(_arb(), broker, HeldBook(), None)
    assert not ok and "exposure" in why
    assert broker.positions == {}  # never half a set


def test_execute_refuses_a_market_held_on_the_other_side(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    held = HeldBook({"A": {"mutually_exclusive:A|OTHER"}}, {"A": "yes"})  # _arb() buys NO A
    ok, why = rrs.execute(_arb(), broker, held, None)
    assert not ok and "other side" in why and broker.positions == {}


def test_a_new_set_can_share_a_market_with_a_held_set(tmp_path):
    # A+B held; A+D is a different set sharing NO A. Each set is hedged on its own.
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    assert rrs.execute(_me_arb(0.20, 0.77, depth=10), broker, HeldBook(), None)[0]
    a = C("A", 0.80, 0.81, bid_size=10)
    d = C("D", 0.30, 0.31, bid_size=10)  # NO D at 0.70: 0.20 + 0.70 = 90c for $1
    ok, why = rrs.execute(relations.relation_arb("mutually_exclusive", a, d), broker, HeldBook.from_broker(broker), None)
    assert ok and why == "filled" and broker.positions["A"]["qty"] == 20

    from kalshi_engine import relation_trading as rt
    sets = {s.group: s for s in rt.held_sets(ledger.load_rows(tmp_path / "f.jsonl"))}
    assert set(sets) == {"mutually_exclusive:A|B", "mutually_exclusive:A|D"}
    assert all(s.qty == 10 for s in sets.values())
    by_set = ledger.build_positions(ledger.load_rows(tmp_path / "f.jsonl"), by_set=True)
    assert sorted(by_set) == ["A@mutually_exclusive:A|B", "A@mutually_exclusive:A|D",
                              "B@mutually_exclusive:A|B", "D@mutually_exclusive:A|D"]


def _me_arb(no_a_ask, no_b_ask, depth=1000.0):
    # NO A + NO B, A and B mutually exclusive: NO asks are 1 - YES bids.
    a = C("A", round(1 - no_a_ask, 4), round(1 - no_a_ask + 0.01, 4), bid_size=depth)
    b = C("B", round(1 - no_b_ask, 4), round(1 - no_b_ask + 0.01, 4), bid_size=depth)
    return relations.relation_arb("mutually_exclusive", a, b)


def test_held_set_is_topped_up_only_at_a_better_price(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    ok, _ = rrs.execute(_me_arb(0.20, 0.77, depth=12), broker, HeldBook(), None)  # 3c/set, 12 sets
    assert ok and broker.positions["A"]["qty"] == 12

    held = HeldBook.from_broker(broker)
    ok, why = rrs.execute(_me_arb(0.20, 0.77, depth=12), broker, held, None)  # same quote again
    assert not ok and "already hold this set" in why

    ok, why = rrs.execute(_me_arb(0.15, 0.79, depth=18), broker, held, None)  # 6c/set: a better price
    assert ok and why.startswith("topped up")
    assert broker.positions["A"]["qty"] == 30 and broker.positions["B"]["qty"] == 30
    rows = [r for r in ledger.load_rows(tmp_path / "f.jsonl") if r["event"] == "fill"]
    assert {r["arb_group"] for r in rows} == {"mutually_exclusive:A|B"} and rows[-1]["topup"] is True


def test_top_up_shrinks_to_the_per_market_limit(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path, max_position_usd=30.0), log_path=tmp_path / "f.jsonl")
    ok, _ = rrs.execute(_me_arb(0.20, 0.77, depth=30), broker, HeldBook(), None)  # B: 30 x 0.77 = $23.10
    assert ok
    ok, why = rrs.execute(_me_arb(0.15, 0.79, depth=100), broker, HeldBook.from_broker(broker), None)
    assert ok and broker.positions["B"]["qty"] == 30 + 8  # ($30 - $23.10) / 0.79 = 8.7 -> 8


# --- Polymarket settlement shape --------------------------------------------

def test_polymarket_settlement_maps_to_kalshi_shape():
    assert polymarket_as_kalshi_shape({"closed": True, "outcomePrices": '["1", "0"]'})["result"] == "yes"
    assert polymarket_as_kalshi_shape({"closed": True, "outcomePrices": '["0", "1"]'})["result"] == "no"
    live = polymarket_as_kalshi_shape({"closed": False, "outcomePrices": '["0.4", "0.6"]', "bestBid": 0.39, "bestAsk": 0.41})
    assert live["status"] == "active" and live["yes_bid_dollars"] == 0.39


def test_missing_quotes_names_the_empty_side():
    # Live case: a nearly-decided BTC strike with no YES bid (so no NO to
    # buy) on A and no YES seller on B.
    a = C("A", 0.0, 0.02)
    b = C("B", 0.99, 1.0)
    assert relations.relation_arb("a_implies_b", a, b) is None
    assert relations.missing_quotes("a_implies_b", a, b) == ["no YES bid on A (so no NO to buy)", "no seller of YES on B"]


# --- prompt versioning ------------------------------------------------------

def test_ranked_list_guidance_is_in_the_not_both_and_at_least_one_questions():
    for rel in ("mutually_exclusive", "exhaustive"):
        assert "DIFFERENT positions" in relations.RELATION_QUESTIONS[rel]
    assert "DIFFERENT positions" not in relations.RELATION_QUESTIONS["a_implies_b"]


def test_only_old_verdicts_accepting_a_stricter_relation_are_reasked():
    def v(version, **kw):
        row = {"route": "typesafe", "probs": _probs(**kw)}
        if version:
            row["prompt_version"] = version
        return row
    assert rrs.needs_reask(v(None, mutually_exclusive=0.95))  # the Moonshot/Zhipu case
    assert not rrs.needs_reask(v(relations.PROMPT_VERSION, mutually_exclusive=0.95))
    assert not rrs.needs_reask(v(None, a_implies_b=0.95))  # implication question unchanged
    assert not rrs.needs_reask(v(None, mutually_exclusive=0.5))  # rejected either way
    assert not rrs.needs_reask(None)


# --- early exit ---------------------------------------------------------------

def _held_pair(tmp_path, **kw):
    from kalshi_engine import relation_trading as rt
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    assert rrs.execute(_me_arb(0.20, 0.77, depth=10), broker, HeldBook(), None)[0]  # 10 sets for $9.70
    (s,) = rt.held_sets(ledger.load_rows(tmp_path / "f.jsonl"))
    return rt, broker, s


def test_exit_when_selling_now_beats_the_guaranteed_payout(tmp_path):
    rt, broker, s = _held_pair(tmp_path)
    assert (s.qty, s.payout_per_set, s.legs) == (10, 1.0, {"A": "no", "B": "no"})
    # NO bids 0.30 + 0.75 = $1.05 a set: more than the $1 it's guaranteed to pay.
    quotes = {"A": C("A", 0.60, 0.70, ask_size=50), "B": C("B", 0.20, 0.25, ask_size=50)}
    ((_, q),) = rt.exit_candidates([s], quotes)
    assert q["proceeds"] == pytest.approx(10.5 - float(ledger_fee(10, 0.30)) - float(ledger_fee(10, 0.75)))
    ok, _ = rt.execute_exit(s, q, broker)
    assert ok and broker.positions == {}
    sells = [r for r in ledger.load_rows(tmp_path / "f.jsonl") if r["event"] == "sell"]
    assert len(sells) == 2 and all(r["arb_group"] == s.group for r in sells)
    assert rt.held_sets(ledger.load_rows(tmp_path / "f.jsonl")) == []


def test_no_exit_below_the_guarantee_or_without_depth(tmp_path):
    rt, _, s = _held_pair(tmp_path)
    below = {"A": C("A", 0.60, 0.72, ask_size=50), "B": C("B", 0.20, 0.25, ask_size=50)}  # 0.28 + 0.75 = 1.03 - fees
    assert rt.exit_candidates([s], below) == []
    thin = {"A": C("A", 0.50, 0.55, ask_size=5), "B": C("B", 0.20, 0.25, ask_size=50)}  # only 5 of 10 sellable
    assert rt.exit_candidates([s], thin) == []


def ledger_fee(qty, price):
    from kalshi_engine.fees import taker_fee
    return taker_fee(qty, price)


# --- plausibility cap: looser for same-venue, same-settlement pairs ---------------

def _sol_pair(settle_a, settle_b, no_above=0.45, no_range=0.19):
    # "SOL above 120" and "SOL 110-120" at noon ET Sep 28: mutually exclusive.
    a = C("PM-sol-above-120", round(1 - no_above, 4), round(1 - no_above + 0.02, 4), venue="polymarket")
    b = C("PM-sol-110-120", round(1 - no_range, 4), round(1 - no_range + 0.02, 4), venue="polymarket")
    a.settlement, b.settlement = settle_a, settle_b
    return relations.relation_arb("mutually_exclusive", a, b)


SOL_KEY = "polymarket|binance.com|2026-09-28T16:00:00Z"


def test_same_settlement_pair_may_carry_a_larger_edge():
    arb = _sol_pair(SOL_KEY, SOL_KEY)  # 0.45 + 0.19: 36c on $1
    assert arb.edge_per_set == pytest.approx(0.36) and arb.tradeable


def test_large_edge_still_blocked_when_settlement_differs_or_is_unknown():
    assert "implausibly large" in _sol_pair(SOL_KEY, "polymarket|binance.com|2026-09-29T16:00:00Z").reason
    assert "implausibly large" in _sol_pair(None, None).reason


def test_same_settlement_still_has_a_ceiling():
    arb = _sol_pair(SOL_KEY, SOL_KEY, no_above=0.30, no_range=0.15)  # 55c on $1
    assert "even for same-settlement" in arb.reason


def test_settlement_keys_from_venue_data():
    from kalshi_engine.relation_sources import kalshi_settlement, polymarket_settlement
    above = {"description": 'Resolves on the Binance 1 minute candle... https://www.binance.com', "endDate": "2026-09-28T16:00:00Z"}
    rng = {"description": "Binance SOL/USDT at https://www.binance.com/en/trade/SOL_USDT with 1m", "endDate": "2026-09-28T16:00:00Z"}
    assert polymarket_settlement(above) == polymarket_settlement(rng) == SOL_KEY
    assert polymarket_settlement({"description": "no link here", "endDate": "x"}) is None
    ev = {"settlement_sources": [{"name": "Netflix Top 10", "url": "https://top10.netflix.com"}]}
    m = {"expected_expiration_time": "2026-09-30T14:00:00Z"}
    assert kalshi_settlement(ev, m) == "kalshi|netflix top 10|2026-09-30T14:00:00Z"
    assert kalshi_settlement({}, m) is None


# --- whole-event groups -------------------------------------------------------------

def test_me_event_arb_uses_every_quoted_market():
    # Three outcomes of a winner-take-all event; NO asks 0.70 + 0.70 + 0.55 = 1.95 < $2.
    ms = [C(t, bid, bid + 0.01, me=True) for t, bid in (("E-A", 0.30), ("E-B", 0.30), ("E-C", 0.45))]
    ms.append(C("E-D", None, 0.02, me=True))  # no bid, so no NO ask: left out, not fatal
    arb = relations.me_event_arb(ms)
    assert arb and len(arb.legs) == 3 and arb.payout_per_set == 2.0 and arb.edge_per_set > 0


def test_partition_arb_buys_yes_on_every_bracket():
    ms = [C(f"B{i}", 0.20, ask, venue="polymarket") for i, ask in enumerate((0.30, 0.30, 0.35))]  # fee-free
    arb = relations.partition_arb(ms)
    assert arb.payout_per_set == 1.0 and [l.side for l in arb.legs] == ["yes"] * 3
    assert arb.edge_per_set == pytest.approx(0.05) and arb.tradeable
    ms[1].yes_ask = None
    assert relations.partition_arb(ms) is None  # a bracket that can't be bought voids the set


# --- walking the book -------------------------------------------------------------

def _pm(ticker, bid_levels, ask_levels):
    c = C(ticker, bid_levels[0][0], ask_levels[0][0], venue="polymarket",
          bid_size=bid_levels[0][1], ask_size=ask_levels[0][1])  # as live quotes set them: from the top level
    c.yes_bid_levels, c.yes_ask_levels = bid_levels, ask_levels
    return c


def test_arb_walks_down_both_books_while_the_next_set_still_pays():
    # Not both: NO A + NO B. NO asks are 1 - YES bids.
    a = _pm("PM-a", [[0.60, 5], [0.58, 10], [0.50, 100]], [[0.62, 5]])   # NO A: 0.40 x5, 0.42 x10, 0.50 x100
    b = _pm("PM-b", [[0.55, 8], [0.52, 50]], [[0.57, 8]])                # NO B: 0.45 x8,  0.48 x50
    arb = relations.relation_arb("mutually_exclusive", a, b)
    # Sets: 5 @ .40+.45, 3 @ .42+.45, 7 @ .42+.48 (=.90), then .50+.48 = .98 still > MIN_EDGE: take it too.
    fills = {l.contract.ticker: l.fills for l in arb.legs}
    assert fills["PM-a"][:2] == [(0.4, 5), (0.42, 10)] and fills["PM-b"][0] == (0.45, 8)
    assert arb.qty > 15 and arb.tradeable
    assert arb.edge_per_set == pytest.approx(1 - sum(l.price for l in arb.legs), abs=1e-3)  # fee-free venue


def test_walk_stops_where_the_next_set_would_lose():
    a = _pm("PM-a", [[0.60, 5], [0.40, 100]], [[0.62, 5]])  # second NO A level at 0.60
    b = _pm("PM-b", [[0.55, 5], [0.30, 100]], [[0.57, 5]])  # second NO B level at 0.70
    arb = relations.relation_arb("mutually_exclusive", a, b)
    assert arb.qty == 5  # 0.60 + 0.70 > $1: the second level isn't bought


def test_without_depth_sizing_is_unchanged():
    a, b = C("A", 0.60, 0.62, bid_size=7), C("B", 0.55, 0.57, bid_size=9)
    arb = relations.relation_arb("mutually_exclusive", a, b)
    assert arb.qty == 7 and all(l.fills is None for l in arb.legs)
