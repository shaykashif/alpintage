import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import score_paper_fills as spf  # noqa: E402
from kalshi_engine import ledger  # noqa: E402

GROUP = "mutually_exclusive:PM-a|PM-b"


def fill(ticker, price, qty=10, **extra):
    return {"event": "fill", "ticker": ticker, "side": "no", "qty": qty, "price": price,
            "cost_usd": price * qty, "strategy": "relation_arb", "venue": "polymarket",
            "relation": "mutually_exclusive", "arb_group": GROUP, "ts": "2026-09-24T00:00:00+00:00", **extra}


def pm(yes_bid, yes_ask, **extra):
    return {"yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask, "status": "active",
            "fee_rate": 0.0, "fee_exponent": 1.0, **extra}


def score(fills, markets):
    positions = ledger.build_positions(fills)
    rows = [spf.score_position(p, markets.get(t)) for t, p in positions.items()]
    sets = spf.mark_arb_sets(rows, positions)
    return {r["ticker"]: r for r in rows}, sets


def test_wide_spread_pair_is_valued_at_its_guaranteed_payout():
    # The Lizzie Borden pair: NO legs at 98.1c + 1.4c, then a 21c-wide book on one leg.
    rows, sets = score(
        [fill("PM-a", 0.981), fill("PM-b", 0.014)],
        {"PM-a": pm(0.03, 0.24), "PM-b": pm(0.947, 0.969)},
    )
    assert rows["PM-a"]["leg_unrealized_pnl"] < -2  # per-leg mark: the spread
    (s,) = sets
    assert s["guaranteed_payout"] == 10.0 and s["value"] == 10.0
    assert abs(s["unrealized_pnl"] - 0.05) < 1e-6
    assert abs(rows["PM-a"]["unrealized_pnl"] + rows["PM-b"]["unrealized_pnl"] - 0.05) < 1e-4
    assert s["at_risk"] == 9.95


def test_set_uses_sale_value_when_it_beats_the_guarantee():
    # Both NO bids high enough that selling now fetches more than $1 a set.
    _, (s,) = score(
        [fill("PM-a", 0.5), fill("PM-b", 0.4)],
        {"PM-a": pm(0.3, 0.35), "PM-b": pm(0.3, 0.35)},
    )
    assert s["liquidation"] == 13.0 and s["value"] == 13.0


def test_settled_leg_payout_counts_toward_the_guarantee():
    # Leg a resolved YES (its NO paid 0), so b's NO must still pay the full $1 a set.
    rows, (s,) = score(
        [fill("PM-a", 0.6), fill("PM-b", 0.3)],
        {"PM-a": {"status": "settled", "result": "yes"}, "PM-b": pm(0.5, 0.6)},
    )
    assert s["guaranteed_payout"] == 10.0
    total = sum(r["realized_pnl"] + r["unrealized_pnl"] for r in rows.values())
    assert abs(total - (10.0 - 9.0)) < 1e-4


def test_partly_sold_leg_breaks_the_hedge():
    sell = {"event": "sell", "ticker": "PM-a", "side": "no", "qty": 5, "proceeds_usd": 4.0,
            "strategy": "relation_arb", "ts": "2026-09-24T01:00:00+00:00"}
    rows, sets = score(
        [fill("PM-a", 0.9), fill("PM-b", 0.05), sell],
        {"PM-a": pm(0.03, 0.24), "PM-b": pm(0.9, 0.96)},
    )
    assert sets == [] and "leg_unrealized_pnl" not in rows["PM-b"]


def test_unpriced_leg_still_gets_the_guarantee():
    rows, (s,) = score([fill("PM-a", 0.9), fill("PM-b", 0.05)], {"PM-b": pm(0.9, 0.96)})
    assert s["liquidation"] is None and s["value"] == 10.0
    assert rows["PM-a"]["status"] == "open"


def test_polymarket_exit_fee_uses_its_own_schedule():
    assert spf.exit_fee({"fee_rate": 0.0}, 10, 0.5) == 0.0
    assert spf.exit_fee({"fee_rate": 0.02, "fee_exponent": 1.0}, 10, 0.5) == 0.05
