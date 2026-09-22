from kalshi_engine.scanner import find_bracket_sum_violations, find_ladder_violations


def _ladder_market(ticker, floor_strike, yes_bid, yes_ask, event="EVT"):
    return {
        "ticker": ticker,
        "event_ticker": event,
        "strike_type": "greater",
        "floor_strike": floor_strike,
        "cap_strike": None,
        "yes_bid_dollars": str(yes_bid),
        "yes_ask_dollars": str(yes_ask),
    }


def _bracket_market(ticker, floor_strike, cap_strike, yes_bid, yes_ask, event="EVT"):
    return {
        "ticker": ticker,
        "event_ticker": event,
        "strike_type": "between",
        "floor_strike": floor_strike,
        "cap_strike": cap_strike,
        "yes_bid_dollars": str(yes_bid),
        "yes_ask_dollars": str(yes_ask),
    }


def test_ladder_flags_a_real_violation():
    markets = [
        _ladder_market("EVT-T100", 100, 0.38, 0.40),  # lower strike, cheap YES
        _ladder_market("EVT-T110", 110, 0.85, 0.90),  # higher strike, expensive YES -- backwards
    ]
    violations = find_ladder_violations(markets)
    assert len(violations) == 1
    v = violations[0]
    assert v.low_ticker == "EVT-T100"
    assert v.high_ticker == "EVT-T110"
    assert v.edge_usd > 0


def test_ladder_no_violation_when_monotonic():
    markets = [
        _ladder_market("EVT-T100", 100, 0.55, 0.60),
        _ladder_market("EVT-T110", 110, 0.25, 0.30),  # correctly cheaper at higher strike
    ]
    assert find_ladder_violations(markets) == []


def test_ladder_ignores_one_sided_quotes():
    markets = [
        _ladder_market("EVT-T100", 100, 0.0, 0.99),  # empty book
        _ladder_market("EVT-T110", 110, 0.90, 0.95),
    ]
    assert find_ladder_violations(markets) == []


def test_bracket_sum_flags_underpriced_set():
    markets = [
        _bracket_market("EVT-B1", 0, 10, 0.28, 0.30),
        _bracket_market("EVT-B2", 10, 20, 0.28, 0.30),
        _bracket_market("EVT-B3", 20, 30, 0.28, 0.30),
    ]  # sum of asks = 0.90, well under $1 even after fees
    violations = find_bracket_sum_violations(markets)
    assert len(violations) == 1
    assert violations[0].edge_usd > 0
    assert set(violations[0].tickers) == {"EVT-B1", "EVT-B2", "EVT-B3"}


def test_bracket_sum_no_violation_when_overpriced():
    markets = [
        _bracket_market("EVT-B1", 0, 10, 0.33, 0.36),
        _bracket_market("EVT-B2", 10, 20, 0.33, 0.36),
        _bracket_market("EVT-B3", 20, 30, 0.33, 0.36),
    ]  # sum of asks = 1.08, no edge
    assert find_bracket_sum_violations(markets) == []
