import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run_cross_venue_scanner as rcvs  # noqa: E402
import score_paper_fills as spf  # noqa: E402
from kalshi_engine import ledger, stat_arb  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.risk import RiskLimits  # noqa: E402


def _limits(tmp_path, **overrides):
    defaults = dict(
        max_order_notional_usd=10.0, max_position_usd=25.0,
        max_total_exposure_usd=100.0, max_daily_loss_usd=20.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


@dataclass
class FakeGame:
    team_probs: dict
    team_asks: dict
    team_tickers: dict
    team_bids: dict = field(default_factory=dict)
    event_ticker: str = "EVT"


# --- stat_arb: sizing and signals ---

def test_kelly_qty_is_capped_by_max_notional():
    # f* = (0.60-0.40)/0.60 = 1/3; quarter-Kelly on $1000 = $83, cap $10 -> 25 contracts
    assert stat_arb.kelly_qty(0.60, 0.40, 1000.0, 10.0) == 25


def test_kelly_qty_zero_without_edge():
    assert stat_arb.kelly_qty(0.40, 0.40, 1000.0, 10.0) == 0
    assert stat_arb.kelly_qty(0.30, 0.40, 1000.0, 10.0) == 0


def test_kelly_qty_small_edge_still_buys_at_least_one():
    assert stat_arb.kelly_qty(0.505, 0.50, 10.0, 10.0) == 1


def test_entry_signal_trades_real_edge_net_of_fee_at_size():
    sig = stat_arb.entry_signal(0.55, 0.50, 1000.0, 10.0)
    assert sig.qty == 20
    # fee at 20 contracts = ceil(0.07*20*.25 = 0.35) -> 0.0175/contract
    assert sig.edge == pytest.approx(0.05 - 0.0175)


def test_entry_signal_rejects_prices_outside_band():
    assert stat_arb.entry_signal(0.10, 0.04, 1000.0, 10.0).qty == 0
    assert stat_arb.entry_signal(0.99, 0.96, 1000.0, 10.0).qty == 0


def test_entry_signal_rejects_implausible_edge():
    sig = stat_arb.entry_signal(0.90, 0.46, 1000.0, 10.0)
    assert sig.qty == 0 and "MAX_PLAUSIBLE_EDGE" in sig.reason


def test_exit_when_bid_converges_to_fair():
    assert stat_arb.exit_reason(fair_now=0.55, bid_now=0.57, qty=20).startswith("converged")


def test_hold_while_spread_still_open():
    assert stat_arb.exit_reason(fair_now=0.55, bid_now=0.50, qty=20) is None


def test_hold_when_bid_touches_fair_but_fee_eats_it():
    # bid == fair, but selling pays a fee, so the net bid is still below fair
    assert stat_arb.exit_reason(fair_now=0.55, bid_now=0.55, qty=20) is None


def test_exit_when_reference_blows_out():
    assert stat_arb.exit_reason(fair_now=0.90, bid_now=0.40, qty=5).startswith("reference broken")


def test_no_exit_into_an_empty_bid():
    assert stat_arb.exit_reason(fair_now=0.10, bid_now=0.0, qty=5) is None


# --- ledger + broker round trip ---

def test_sell_realizes_pnl_against_fee_inclusive_cost(tmp_path):
    log = tmp_path / "fills.jsonl"
    broker = PaperBroker(limits=_limits(tmp_path), log_path=log)
    broker.buy("T", "yes", 0.50, qty=10, strategy="stat_arb", venue="odds_api", fair=0.55)
    # entry fee ceil(0.07*10*.25=0.175) = 0.18 -> cost 5.18
    fill = broker.sell("T", 0.56, reason="converged")
    assert fill is not None and "T" not in broker.positions
    # exit fee ceil(0.07*10*.56*.44=0.17248) = 0.18 -> proceeds 5.42
    pos = ledger.build_positions(ledger.load_rows(log))["T"]
    assert pos.qty_open == 0
    assert pos.realized_pnl_usd == pytest.approx(5.42 - 5.18)
    assert pos.meta["venue"] == "odds_api"
    assert broker.cash_usd == pytest.approx(10000 - 5.18 + 5.42)


def test_sell_with_nothing_held_is_a_noop(tmp_path):
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "fills.jsonl")
    assert broker.sell("NOPE", 0.5) is None


def test_from_ledger_restores_open_positions_and_cash(tmp_path):
    log = tmp_path / "fills.jsonl"
    first = PaperBroker(limits=_limits(tmp_path), log_path=log)
    first.buy("A", "yes", 0.40, qty=5)
    first.buy("B", "yes", 0.30, qty=5)
    first.sell("B", 0.35)

    replayed = PaperBroker.from_ledger(limits=_limits(tmp_path), log_path=log)
    assert set(replayed.positions) == {"A"}
    assert replayed.positions["A"]["qty"] == 5
    assert replayed.cash_usd == pytest.approx(first.cash_usd)


def test_from_ledger_closes_settled_positions(tmp_path):
    log = tmp_path / "fills.jsonl"
    first = PaperBroker(limits=_limits(tmp_path), log_path=log)
    first.buy("A", "yes", 0.40, qty=5)
    replayed = PaperBroker.from_ledger(limits=_limits(tmp_path), log_path=log, settled={"A": 5.0})
    assert replayed.positions == {}
    assert replayed.cash_usd == pytest.approx(first.cash_usd + 5.0)


def test_legacy_rows_without_strategy_field_are_classified():
    rows = [
        {"event": "fill", "ticker": "X", "side": "yes", "qty": 1, "cost_usd": 0.4,
         "reason": "cross-venue stat-arb: odds_api (POC mode), edge=0.06"},
        {"event": "fill", "ticker": "Y", "side": "yes", "qty": 1, "cost_usd": 0.4, "reason": "ladder violation"},
    ]
    pos = ledger.build_positions(rows)
    assert pos["X"].strategy == "stat_arb" and pos["X"].meta["venue"] == "odds_api"
    assert pos["Y"].strategy == "ladder_bracket"


# --- scanner: entry sizing and exit pass ---

def test_maybe_trade_sizes_with_kelly_and_tags_the_fill(tmp_path, monkeypatch):
    kg = FakeGame(
        team_probs={"TeamA": 0.50, "TeamB": 0.50},
        team_asks={"TeamA": 0.50, "TeamB": 0.52},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.55, "TeamB": 0.45}, "polymarket_probs": None, "odds_api_team_map": {"TeamA": "TeamA", "TeamB": "TeamB"}}
    broker = PaperBroker(limits=_limits(tmp_path), log_path=tmp_path / "fills.jsonl")

    rcvs._maybe_trade(kg, row, broker, set())

    assert broker.positions["TICK-A"]["qty"] == 20
    fill_row = ledger.load_rows(tmp_path / "fills.jsonl")[-1]
    assert fill_row["strategy"] == "stat_arb" and fill_row["venue"] == "odds_api" and fill_row["fair"] == 0.55


def test_maybe_exit_closes_converged_position(tmp_path):
    log = tmp_path / "fills.jsonl"
    broker = PaperBroker(limits=_limits(tmp_path), log_path=log)
    broker.buy("TICK-A", "yes", 0.50, qty=20, strategy="stat_arb", venue="odds_api", fair=0.55)
    held = ledger.open_positions(ledger.load_rows(log))

    kg = FakeGame(
        team_probs={"TeamA": 0.575, "TeamB": 0.425},
        team_asks={"TeamA": 0.58, "TeamB": 0.43},
        team_bids={"TeamA": 0.57, "TeamB": 0.42},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.55, "TeamB": 0.45}, "polymarket_probs": None, "odds_api_team_map": {"TeamA": "TeamA", "TeamB": "TeamB"}}

    rcvs._maybe_exit(kg, row, broker, held)

    assert "TICK-A" not in broker.positions
    assert ledger.load_rows(log)[-1]["event"] == "sell"


def test_maybe_exit_holds_while_spread_open(tmp_path):
    log = tmp_path / "fills.jsonl"
    broker = PaperBroker(limits=_limits(tmp_path), log_path=log)
    broker.buy("TICK-A", "yes", 0.50, qty=20, strategy="stat_arb", venue="odds_api", fair=0.55)
    held = ledger.open_positions(ledger.load_rows(log))

    kg = FakeGame(
        team_probs={"TeamA": 0.505, "TeamB": 0.495},
        team_asks={"TeamA": 0.51, "TeamB": 0.50},
        team_bids={"TeamA": 0.50, "TeamB": 0.49},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.55, "TeamB": 0.45}, "polymarket_probs": None, "odds_api_team_map": {"TeamA": "TeamA", "TeamB": "TeamB"}}

    rcvs._maybe_exit(kg, row, broker, held)

    assert broker.positions["TICK-A"]["qty"] == 20


# --- scorer: mark-to-market and settlement ---

def _pos(qty=10, cost=5.18, sold=0, realized=0.0):
    p = ledger.Position(ticker="T", side="yes", strategy="stat_arb", qty_bought=qty, qty_sold=sold,
                        cost_usd=cost, realized_pnl_usd=realized)
    return p


def test_open_position_is_marked_at_bid_net_of_exit_fee():
    row = spf.score_position(_pos(), {"status": "active", "yes_bid_dollars": "0.56", "yes_ask_dollars": "0.57"})
    assert row["status"] == "open"
    assert row["unrealized_pnl"] == pytest.approx(5.6 - 0.18 - 5.18)


def test_settled_winner_realizes_payout():
    row = spf.score_position(_pos(), {"status": "settled", "result": "yes"})
    assert row["status"] == "settled" and row["realized_pnl"] == pytest.approx(10 - 5.18)


def test_exited_position_needs_no_market():
    row = spf.score_position(_pos(sold=10, realized=0.24), None)
    assert row["status"] == "exited" and row["realized_pnl"] == 0.24


def test_summarize_splits_by_strategy_and_counts_wins():
    rows = [
        spf.score_position(_pos(sold=10, realized=0.24), None),
        spf.score_position(_pos(), {"status": "settled", "result": "no"}),
    ]
    s = spf.summarize(rows, "now")
    assert s["closed_count"] == 2 and s["wins"] == 1
    assert s["realized_pnl"] == pytest.approx(0.24 - 5.18, abs=0.01)
    assert s["by_strategy"]["stat_arb"]["positions"] == 2
