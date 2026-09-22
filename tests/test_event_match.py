from kalshi_engine.event_match import dates_close
from kalshi_engine.polymarket_client import parse_outcomes


def test_dates_close_within_window():
    assert dates_close("2026-09-28T00:15:00Z", "2026-09-28T17:00:00Z", max_hours=36)


def test_dates_close_rejects_far_apart():
    assert not dates_close("2026-09-28T00:15:00Z", "2026-10-05T00:15:00Z", max_hours=36)


def test_dates_close_handles_missing():
    assert not dates_close(None, "2026-09-28T00:15:00Z")
    assert not dates_close("2026-09-28T00:15:00Z", None)


def test_parse_outcomes_from_json_strings():
    market = {"outcomes": '["Giants", "Rams"]', "outcomePrices": '["0.086", "0.914"]'}
    parsed = parse_outcomes(market)
    assert parsed == {"Giants": 0.086, "Rams": 0.914}
