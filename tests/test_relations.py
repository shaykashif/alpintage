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
from kalshi_engine.risk import RiskLimits  # noqa: E402


def C(ticker, bid, ask, event=None, title="", venue="kalshi", bid_size=1000.0, ask_size=1000.0, me=False, fee_rate=0.0):
    return relations.Contract(
        venue=venue, ticker=ticker, event_id=event or ticker, category="Entertainment",
        title=title or ticker, context=f"rules for {ticker}", close_time=None,
        yes_bid=bid, yes_ask=ask, yes_bid_size=bid_size, yes_ask_size=ask_size,
        event_mutually_exclusive=me, fee_rate=fee_rate,
    )


def _limits(tmp_path, **kw):
    d = dict(max_order_notional_usd=10.0, max_position_usd=25.0, max_total_exposure_usd=100.0,
             max_daily_loss_usd=20.0, kill_switch_path=tmp_path / "KILL_SWITCH")
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
    assert arb.qty == 20  # $10 notional cap / $0.50 leg


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


def test_implications_use_the_lower_gate_other_relations_do_not():
    assert relations.classify(_probs(a_implies_b=0.85, same_underlying=0.82))[0] == ["a_implies_b"]
    assert relations.classify(_probs(a_implies_b=0.9, b_implies_a=0.9, same_underlying=0.82))[0] == ["a_implies_b", "b_implies_a"]
    assert relations.classify(_probs(b_implies_a=0.85, same_underlying=0.79))[0] == []
    assert relations.classify(_probs(mutually_exclusive=0.9, same_underlying=0.85))[0] == []
    assert relations.classify(_probs(exhaustive=0.9, same_underlying=0.85))[0] == []


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
    ok, _ = rrs.execute(_arb(), broker, set(), {"probs": {"a_implies_b": 0.9}})
    assert ok and set(broker.positions) == {"A", "PM-b"}
    rows = ledger.load_rows(tmp_path / "f.jsonl")
    assert {r["strategy"] for r in rows} == {"relation_arb"}
    pm_row = next(r for r in rows if r["ticker"] == "PM-b")
    assert pm_row["fee_usd"] == pytest.approx(0.05 * 20 * 0.5 * 0.5)  # Polymarket's fee, not Kalshi's


def test_execute_buys_nothing_if_the_whole_set_would_breach_exposure(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path, max_total_exposure_usd=12.0), log_path=tmp_path / "f.jsonl")
    ok, why = rrs.execute(_arb(), broker, set(), None)
    assert not ok and "exposure" in why
    assert broker.positions == {}  # never half a set


def test_execute_skips_when_a_leg_is_already_held(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "f.jsonl")
    ok, why = rrs.execute(_arb(), broker, {"A"}, None)
    assert not ok and broker.positions == {}


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


# --- structural veto: ranked lists -----------------------------------------

def _ranked(ticker, title, venue="polymarket"):
    return relations.Contract(venue=venue, ticker=ticker, event_id=ticker, category=None, title=title,
                              context=f"Question: {title}\nRules: ties for second or third place resolve per the list.",
                              close_time=None, yes_bid=None, yes_ask=None)


def test_rank_extraction_needs_ranking_context():
    assert relations.list_ranks(_ranked("x", "Will Moonshot be the third-best Chinese AI company?")) == {3}
    assert relations.list_ranks(_ranked("x", "Will zptai be the second best Chinese AI company?")) == {2}
    assert relations.list_ranks(_ranked("x", "Will this song be #1 on the Hot 100?")) == {1}
    assert relations.list_ranks(_ranked("x", "Will X finish in 2nd place?")) == {2}
    assert relations.list_ranks(_ranked("x", "First quarter GDP above 2%?")) == set()
    assert relations.list_ranks(_ranked("x", "Will the second round be held?")) == set()


def test_not_both_vetoed_between_different_list_positions():
    # The live case: #3 and #2 on the same list can both be YES.
    a = _ranked("PM-moonshot", "Will Moonshot be the third best Chinese AI company at the end of September 2026?")
    b = _ranked("PM-zptai", "Will zptai be the second best Chinese AI company at the end of September 2026?")
    assert "different list positions" in relations.structural_veto(["mutually_exclusive"], a, b)
    assert relations.structural_veto(["mutually_exclusive", "exhaustive"], a, b)


def test_not_both_kept_for_the_same_position_and_implications_untouched():
    a = _ranked("PM-a", "Will Moonshot be the second best Chinese AI company?")
    b = _ranked("PM-b", "Will zptai be the second best Chinese AI company?")
    assert relations.structural_veto(["mutually_exclusive"], a, b) is None
    c = _ranked("PM-c", "Will zptai be the third best Chinese AI company?")
    assert relations.structural_veto(["a_implies_b"], a, c) is None
    plain = _ranked("PM-d", "Will the Fed cut rates in October?")
    assert relations.structural_veto(["mutually_exclusive"], a, plain) is None
