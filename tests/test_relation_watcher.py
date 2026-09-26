import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run_relation_watcher as rrw  # noqa: E402
from kalshi_engine import ledger, relation_trading as rt, relations  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.relation_sources import Quote  # noqa: E402
from kalshi_engine.risk import RiskLimits  # noqa: E402


def C(ticker, venue="kalshi"):
    return relations.Contract(venue=venue, ticker=ticker, event_id=ticker, category=None, title=ticker,
                              context=f"rules for {ticker}", close_time=None, yes_bid=None, yes_ask=None)


def _setup(tmp_path, monkeypatch, quotes_by_tick):
    """Wires the watcher to tmp files and a scripted sequence of quotes."""
    ticks = iter(quotes_by_tick)
    arbs: list[dict] = []
    fills = tmp_path / "fills.jsonl"
    limits = RiskLimits(max_order_notional_usd=100.0, max_position_usd=100.0, max_total_exposure_usd=1000.0,
                        max_daily_loss_usd=200.0, kill_switch_path=tmp_path / "KILL_SWITCH")
    real_lock = ledger.ledger_lock
    monkeypatch.setattr(rrw, "fetch_quotes", lambda contracts, client: next(ticks))
    monkeypatch.setattr(rrw, "LIVE_PATH", tmp_path / "live.json")
    monkeypatch.setattr(rrw, "append_arb_rows", arbs.extend)
    monkeypatch.setattr(rrw, "settled_payouts", dict)
    monkeypatch.setattr(ledger, "ledger_lock", lambda: real_lock(tmp_path / "ledger.lock"))
    monkeypatch.setattr(rrw.PaperBroker, "from_ledger",
                        classmethod(lambda cls, settled=None: PaperBroker(limits=limits, log_path=fills)))
    return arbs, fills


def _pairs(tmp_path):
    a, b = C("A"), C("PM-b", venue="polymarket")
    rt.write_watchlist([(a, b, ["mutually_exclusive"], {"probs": {"mutually_exclusive": 0.95}})], tmp_path / "w.json")
    return rt.load_watchlist(tmp_path / "w.json")


# NO A costs 1 - 0.60 = 0.40, NO b costs 1 - 0.55 = 0.45: 0.85 for a set paying $1.
VIOLATION = {"A": Quote(0.60, 0.62, 100, 100), "PM-b": Quote(0.55, 0.57)}
CONSISTENT = {"A": Quote(0.40, 0.42, 100, 100), "PM-b": Quote(0.45, 0.47)}


def test_watchlist_round_trips_without_rules_text_and_shares_contracts(tmp_path):
    a, b, c = C("A"), C("B"), C("PM-c", venue="polymarket")
    rt.write_watchlist([(a, b, ["a_implies_b"], {"probs": {}}), (a, c, ["exhaustive"], {"probs": {}})], tmp_path / "w.json")
    pairs = rt.load_watchlist(tmp_path / "w.json")
    assert [p["relations"] for p in pairs] == [["a_implies_b"], ["exhaustive"]]
    assert pairs[0]["a"] is pairs[1]["a"]  # one quote update reaches every pair the market is in
    assert pairs[0]["a"].context == ""
    assert rt.load_watchlist(tmp_path / "missing.json") == []


def test_violation_is_logged_and_traded_once_while_it_persists(tmp_path, monkeypatch):
    arbs, fills = _setup(tmp_path, monkeypatch, [(VIOLATION, []), (VIOLATION, []), (CONSISTENT, [])])
    pairs, signals = _pairs(tmp_path), set()
    live, _ = rrw.tick(pairs, None, signals, paper=True)
    assert live["rows"][0]["status"] == "violation" and live["rows"][0]["edge"] > 0
    assert len(arbs) == 1 and arbs[0]["traded"] and arbs[0]["source"] == "watch"
    assert {r["ticker"] for r in ledger.load_rows(fills)} == {"A", "PM-b"}

    rrw.tick(pairs, None, signals, paper=True)  # same prices: no new log line, no new buy
    assert len(arbs) == 1 and len(ledger.load_rows(fills)) == 2

    live, _ = rrw.tick(pairs, None, signals, paper=True)
    assert live["rows"][0]["status"] == "consistent" and not signals
    assert json.loads((tmp_path / "live.json").read_text())["counts"] == {"consistent": 1}


def test_failed_venue_clears_its_quotes_so_nothing_stale_trades(tmp_path, monkeypatch):
    only_kalshi = {"A": VIOLATION["A"]}  # Polymarket fetch failed this tick
    arbs, fills = _setup(tmp_path, monkeypatch, [(VIOLATION, []), (only_kalshi, ["polymarket: boom"])])
    pairs, signals = _pairs(tmp_path), set()
    rrw.tick(pairs, None, set(), paper=False)  # loads VIOLATION quotes into the contracts
    live, _ = rrw.tick(pairs, None, signals, paper=True)
    assert live["rows"][0]["status"] == "unpriced" and live["errors"] == ["polymarket: boom"]
    assert pairs[0]["b"].yes_bid is None
    assert not ledger.load_rows(fills)


def test_without_paper_it_logs_but_never_trades(tmp_path, monkeypatch):
    arbs, fills = _setup(tmp_path, monkeypatch, [(VIOLATION, [])])
    rrw.tick(_pairs(tmp_path), None, set(), paper=False)
    assert len(arbs) == 1 and not arbs[0]["traded"]
    assert not ledger.load_rows(fills)


def test_ledger_lock_is_exclusive_and_breaks_stale_locks(tmp_path):
    lock = tmp_path / "l.lock"
    with ledger.ledger_lock(lock):
        try:
            with ledger.ledger_lock(lock, timeout_s=0.1):
                raise AssertionError("second holder got the lock")
        except TimeoutError:
            pass
    assert not lock.exists()
    lock.write_text("123")  # left behind by a crashed holder
    os.utime(lock, (0, 0))
    with ledger.ledger_lock(lock, timeout_s=0.1, stale_s=1.0):
        pass


def test_only_pairs_touching_a_moved_market_are_repriced(tmp_path, monkeypatch):
    monkeypatch.setattr(rrw, "LIVE_PATH", tmp_path / "live.json")
    a, b, c, d = C("A"), C("B"), C("C"), C("D")
    rt.write_watchlist([(a, b, ["mutually_exclusive"], {}), (c, d, ["mutually_exclusive"], {})], tmp_path / "w.json")
    pairs = rt.load_watchlist(tmp_path / "w.json")
    contracts = rrw.unique_contracts(pairs)
    calls = []
    real = rrw.price_relations
    monkeypatch.setattr(rrw, "price_relations", lambda x, y, r: calls.append(x.ticker) or real(x, y, r))
    quotes = {t: Quote(0.40, 0.42, 10, 10) for t in "ABCD"}
    memo: dict = {}
    rrw.price_and_trade(pairs, contracts, quotes, [], set(), False, dirty=set(), memo=memo)
    assert sorted(calls) == ["A", "C"]  # first pass prices everything
    calls.clear()
    live, _ = rrw.price_and_trade(pairs, contracts, quotes, [], set(), False, dirty={"D"}, memo=memo)
    assert calls == ["C"] and len(live["rows"]) == 2  # only the C/D pair; A/B reused
