from pathlib import Path

import pytest

from kalshi_engine.paper_broker import PaperBroker
from kalshi_engine.risk import RiskLimits, RiskState, RiskVeto, check_order


def _limits(tmp_path, **overrides):
    defaults = dict(
        max_order_notional_usd=10.0,
        max_position_usd=25.0,
        max_total_exposure_usd=100.0,
        max_daily_loss_usd=20.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


def _empty_state(**overrides):
    defaults = dict(position_usd_by_ticker={}, total_exposure_usd=0.0, realized_pnl_today_usd=0.0)
    defaults.update(overrides)
    return RiskState(**defaults)


# --- risk.check_order ---

def test_order_within_limits_is_allowed(tmp_path):
    check_order(_limits(tmp_path), _empty_state(), "TICK", notional_usd=5.0)  # should not raise


def test_order_over_max_notional_is_vetoed(tmp_path):
    with pytest.raises(RiskVeto, match="order notional"):
        check_order(_limits(tmp_path), _empty_state(), "TICK", notional_usd=15.0)


def test_order_that_breaches_position_limit_is_vetoed(tmp_path):
    state = _empty_state(position_usd_by_ticker={"TICK": 20.0})
    with pytest.raises(RiskVeto, match="position"):
        check_order(_limits(tmp_path), state, "TICK", notional_usd=10.0)  # 20 + 10 > 25


def test_order_that_breaches_total_exposure_is_vetoed(tmp_path):
    state = _empty_state(total_exposure_usd=95.0)
    with pytest.raises(RiskVeto, match="exposure"):
        check_order(_limits(tmp_path), state, "TICK", notional_usd=10.0)  # 95 + 10 > 100


def test_order_after_daily_loss_limit_hit_is_vetoed(tmp_path):
    state = _empty_state(realized_pnl_today_usd=-20.0)
    with pytest.raises(RiskVeto, match="daily loss"):
        check_order(_limits(tmp_path), state, "TICK", notional_usd=5.0)


def test_kill_switch_file_vetoes_everything(tmp_path):
    limits = _limits(tmp_path)
    limits.kill_switch_path.touch()
    with pytest.raises(RiskVeto, match="kill switch"):
        check_order(limits, _empty_state(), "TICK", notional_usd=1.0)


# --- PaperBroker ---

def test_broker_fills_a_normal_order(tmp_path):
    broker = PaperBroker(
        limits=_limits(tmp_path), cash_usd=1000.0, log_path=tmp_path / "fills.jsonl"
    )
    fill = broker.buy("TICK", "yes", price=0.40, qty=1)
    assert fill is not None
    assert fill.price == 0.40
    assert broker.positions["TICK"]["qty"] == 1
    assert broker.cash_usd < 1000.0  # price + fee deducted


def test_broker_vetoes_order_over_position_limit(tmp_path):
    limits = _limits(tmp_path, max_position_usd=1.0, max_order_notional_usd=100.0)
    broker = PaperBroker(limits=limits, cash_usd=1000.0, log_path=tmp_path / "fills.jsonl")
    fill = broker.buy("TICK", "yes", price=0.50, qty=10)  # notional $5 > $1 position cap
    assert fill is None
    assert "TICK" not in broker.positions
    assert broker.cash_usd == 1000.0  # nothing spent


def test_broker_respects_kill_switch(tmp_path):
    limits = _limits(tmp_path)
    broker = PaperBroker(limits=limits, cash_usd=1000.0, log_path=tmp_path / "fills.jsonl")
    limits.kill_switch_path.touch()
    fill = broker.buy("TICK", "yes", price=0.40, qty=1)
    assert fill is None


def test_broker_vetoes_when_cash_insufficient(tmp_path):
    limits = _limits(tmp_path, max_order_notional_usd=1000.0, max_position_usd=1000.0, max_total_exposure_usd=1000.0)
    broker = PaperBroker(limits=limits, cash_usd=5.0, log_path=tmp_path / "fills.jsonl")
    fill = broker.buy("TICK", "yes", price=0.90, qty=10)  # costs $9+ , only has $5
    assert fill is None
    assert broker.cash_usd == 5.0


def test_broker_logs_fills_and_vetoes(tmp_path):
    log_path = tmp_path / "fills.jsonl"
    limits = _limits(tmp_path, max_position_usd=1.0, max_order_notional_usd=100.0)
    broker = PaperBroker(limits=limits, cash_usd=1000.0, log_path=log_path)
    broker.buy("TICK", "yes", price=0.50, qty=10)  # vetoed
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert '"event": "veto"' in lines[0]
