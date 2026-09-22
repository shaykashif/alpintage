import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run_cross_venue_scanner as rcvs  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.risk import RiskLimits  # noqa: E402


@dataclass
class FakeGame:
    team_probs: dict
    team_asks: dict
    team_tickers: dict


def _broker(tmp_path):
    limits = RiskLimits(
        max_order_notional_usd=100.0, max_position_usd=100.0,
        max_total_exposure_usd=1000.0, max_daily_loss_usd=100.0,
        kill_switch_path=tmp_path / "KILL_SWITCH",
    )
    return PaperBroker(limits=limits, cash_usd=1000.0, log_path=tmp_path / "fills.jsonl")


def test_no_trade_when_venue_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (False, "not enough data"))
    kg = FakeGame(
        team_probs={"TeamA": 0.40, "TeamB": 0.60},
        team_asks={"TeamA": 0.40, "TeamB": 0.62},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.60, "TeamB": 0.40}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())

    assert broker.positions == {}
    assert broker.cash_usd == 1000.0


def test_trades_when_trusted_and_edge_clears_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "beats Kalshi on 40 games"))
    kg = FakeGame(
        team_probs={"TeamA": 0.40, "TeamB": 0.60},
        team_asks={"TeamA": 0.40, "TeamB": 0.62},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    # venue thinks TeamA is 0.60 likely, Kalshi's ask for TeamA is only 0.40 -- a large edge
    row = {"odds_api_avg_probs": {"TeamA": 0.60, "TeamB": 0.40}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())

    assert "TICK-A" in broker.positions
    assert broker.positions["TICK-A"]["side"] == "yes"


def test_no_trade_when_edge_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "beats Kalshi on 40 games"))
    kg = FakeGame(
        team_probs={"TeamA": 0.50, "TeamB": 0.50},
        team_asks={"TeamA": 0.50, "TeamB": 0.52},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    # venue only slightly disagrees -- edge should be under MIN_TRADE_EDGE
    row = {"odds_api_avg_probs": {"TeamA": 0.505, "TeamB": 0.495}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())

    assert broker.positions == {}


def test_skips_tickers_already_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "beats Kalshi on 40 games"))
    kg = FakeGame(
        team_probs={"TeamA": 0.40, "TeamB": 0.60},
        team_asks={"TeamA": 0.40, "TeamB": 0.62},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.60, "TeamB": 0.40}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, {"TICK-A"})  # already filled

    assert broker.positions == {}
