import math

from kalshi_engine.surebet import OddsQuote, find_surebet


def test_finds_a_real_surebet_across_two_books():
    quotes = [
        OddsQuote(bookmaker="BookA", outcome="Team1", american_odds=150),  # decimal 2.50
        OddsQuote(bookmaker="BookB", outcome="Team2", american_odds=120),  # decimal 2.20
    ]
    result = find_surebet(quotes, total_stake_usd=100.0)
    assert result is not None
    assert result.profit_usd > 0
    # payout is the same no matter which leg wins
    payouts = {leg.payout_if_wins_usd for leg in result.legs}
    assert len(payouts) == 1
    assert math.isclose(sum(leg.stake_usd for leg in result.legs), 100.0, abs_tol=0.02)


def test_no_surebet_on_a_normal_vigged_line():
    quotes = [
        OddsQuote(bookmaker="BookA", outcome="Team1", american_odds=-110),
        OddsQuote(bookmaker="BookA", outcome="Team2", american_odds=-110),
    ]
    assert find_surebet(quotes) is None


def test_picks_best_price_across_multiple_quotes_per_outcome():
    quotes = [
        OddsQuote(bookmaker="BookA", outcome="Team1", american_odds=-200),  # worse
        OddsQuote(bookmaker="BookB", outcome="Team1", american_odds=150),  # better, should be picked
        OddsQuote(bookmaker="BookC", outcome="Team2", american_odds=120),
    ]
    result = find_surebet(quotes)
    assert result is not None
    team1_leg = next(leg for leg in result.legs if leg.outcome == "Team1")
    assert team1_leg.bookmaker == "BookB"
    assert team1_leg.american_odds == 150


def test_needs_at_least_two_outcomes():
    quotes = [OddsQuote(bookmaker="BookA", outcome="Team1", american_odds=150)]
    assert find_surebet(quotes) is None
