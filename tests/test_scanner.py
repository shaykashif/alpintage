from kalshi_engine.scanner import complete_bracket_set, find_bracket_sum_violations, find_ladder_violations


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


def _tail(ticker, strike_type, strike, yes_bid, yes_ask, event="EVT"):
    return {
        "ticker": ticker,
        "event_ticker": event,
        "strike_type": strike_type,
        "floor_strike": strike if strike_type == "greater" else None,
        "cap_strike": strike if strike_type == "less" else None,
        "yes_bid_dollars": str(yes_bid),
        "yes_ask_dollars": str(yes_ask),
    }


def _temperature_event(mid_ask=0.20, tail_ask=0.05):
    # Kalshi's real temperature layout (verified on KXHIGHTDAL-26SEP24):
    # "92 or below", 93-94, 95-96, 97-98, 99-100, "101 or above"
    return [
        _tail("EVT-T93", "less", 93, tail_ask - 0.01, tail_ask),
        _bracket_market("EVT-B93.5", 93, 94, mid_ask - 0.01, mid_ask),
        _bracket_market("EVT-B95.5", 95, 96, mid_ask - 0.01, mid_ask),
        _bracket_market("EVT-B97.5", 97, 98, mid_ask - 0.01, mid_ask),
        _bracket_market("EVT-B99.5", 99, 100, mid_ask - 0.01, mid_ask),
        _tail("EVT-T100", "greater", 100, tail_ask - 0.01, tail_ask),
    ]


def test_bracket_sum_flags_underpriced_complete_set():
    markets = _temperature_event(mid_ask=0.20, tail_ask=0.02)  # 0.84 + fees < $1
    violations = find_bracket_sum_violations(markets)
    assert len(violations) == 1
    v = violations[0]
    assert v.edge_usd > 0
    assert set(v.tickers) == {m["ticker"] for m in markets}  # tails included
    assert v.leg_asks["EVT-T93"] == 0.02


def test_bracket_sum_no_violation_when_overpriced():
    assert find_bracket_sum_violations(_temperature_event(mid_ask=0.24, tail_ask=0.05)) == []


def test_trump_approval_sep23_partial_set_is_not_an_arb():
    # Replays the real loss: the old check bought only the 7 "between"
    # buckets (asks summing to $0.84), ignored the "Below 39.6%" and
    # "Above 40.2%" tails, and lost everything when "Below 39.6%" won.
    ev = "KXTRUMPAPPROVE-26SEP23"
    asks = {39.6: 0.20, 39.7: 0.23, 39.8: 0.14, 39.9: 0.11, 40.0: 0.07, 40.1: 0.04, 40.2: 0.03}
    mids = [_bracket_market(f"{ev}-E{k}", k, k, a - 0.01, a, event=ev) for k, a in asks.items()]
    below = _tail(f"{ev}-U39.6", "less", 39.6, 0.40, 0.42, event=ev)
    above = _tail(f"{ev}-A40.2", "greater", 40.2, 0.01, 0.02, event=ev)

    # Middle buckets alone: no longer flagged -- the set isn't exhaustive.
    assert find_bracket_sum_violations(mids) == []
    # Full set: $0.84 + $0.42 + $0.02 = $1.28 -- correctly no arb at all.
    assert find_bracket_sum_violations(mids + [below, above]) == []


def test_missing_middle_bucket_is_incomplete():
    markets = _temperature_event(mid_ask=0.15, tail_ask=0.02)
    del markets[2]  # drop 95-96: tails still present, but there's a hole
    ok, why = complete_bracket_set(markets)
    assert ok is None and "contiguous" in why


def test_leg_without_an_ask_blocks_the_whole_set():
    markets = _temperature_event(mid_ask=0.15, tail_ask=0.02)
    markets[-1]["yes_ask_dollars"] = "1.00"  # "above" tail has no seller
    ok, why = complete_bracket_set(markets)
    assert ok is None and "EVT-T100" in why
    assert find_bracket_sum_violations(markets) == []


def test_approval_style_single_value_buckets_are_complete():
    # floor == cap for each bucket, 0.1 ticks -- verified on KXTRUMPAPPROVE-26SEP24
    ev = "E"
    mids = [_bracket_market(f"E-{k:.1f}", round(k, 1), round(k, 1), 0.05, 0.06, event=ev)
            for k in [38.7 + 0.1 * i for i in range(7)]]
    legs, why = complete_bracket_set(mids + [_tail("E-U", "less", 38.7, 0.1, 0.11, event=ev),
                                             _tail("E-A", "greater", 39.3, 0.1, 0.11, event=ev)])
    assert legs is not None, why
    assert len(legs) == 9
