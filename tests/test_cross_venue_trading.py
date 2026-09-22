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


def _big_edge_game():
    return FakeGame(
        team_probs={"TeamA": 0.40, "TeamB": 0.60},
        team_asks={"TeamA": 0.40, "TeamB": 0.62},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    ), {"odds_api_avg_probs": {"TeamA": 0.60, "TeamB": 0.40}, "polymarket_probs": None}


# --- default (POC) mode: trades on edge alone, no trust check ---

def test_poc_mode_trades_on_edge_alone_without_checking_trust(tmp_path, monkeypatch):
    def _explode(venue_key):
        raise AssertionError("venue_trust should not be called in POC mode")
    monkeypatch.setattr(rcvs, "venue_trust", _explode)

    kg, row = _big_edge_game()
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())  # require_trust defaults False

    assert "TICK-A" in broker.positions
    assert broker.positions["TICK-A"]["side"] == "yes"


def test_poc_mode_no_trade_when_edge_too_small(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "irrelevant in POC mode"))
    kg = FakeGame(
        team_probs={"TeamA": 0.50, "TeamB": 0.50},
        team_asks={"TeamA": 0.50, "TeamB": 0.52},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.505, "TeamB": 0.495}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())

    assert broker.positions == {}


def test_poc_mode_skips_tickers_already_filled(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "irrelevant"))
    kg, row = _big_edge_game()
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, {"TICK-A"})

    assert broker.positions == {}


def test_implausibly_large_edge_is_skipped_not_traded(tmp_path, monkeypatch):
    # Caught live: a mismatched game (same two teams, wrong day of a
    # multi-game series) produced a fake 44.5-point edge that must never
    # silently trade, even in POC mode.
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "irrelevant in POC mode"))
    kg = FakeGame(
        team_probs={"TeamA": 0.455, "TeamB": 0.545},
        team_asks={"TeamA": 0.46, "TeamB": 0.55},
        team_tickers={"TeamA": "TICK-A", "TeamB": "TICK-B"},
    )
    row = {"odds_api_avg_probs": {"TeamA": 0.90, "TeamB": 0.10}, "polymarket_probs": None}
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set())

    assert broker.positions == {}
    assert broker.cash_usd == 1000.0


# --- require_trust=True: the stricter, evidence-gated mode ---

def test_require_trust_blocks_trade_when_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (False, "not enough data"))
    kg, row = _big_edge_game()
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set(), require_trust=True)

    assert broker.positions == {}
    assert broker.cash_usd == 1000.0


def test_require_trust_allows_trade_when_trusted_and_edge_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(rcvs, "venue_trust", lambda venue_key: (True, "beats Kalshi on 40 games"))
    kg, row = _big_edge_game()
    broker = _broker(tmp_path)

    rcvs._maybe_trade(kg, row, broker, set(), require_trust=True)

    assert "TICK-A" in broker.positions
