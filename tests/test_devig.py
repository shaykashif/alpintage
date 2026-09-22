import math

from kalshi_engine.devig import (
    BookLine,
    aggregate_fair_probs,
    american_to_prob,
    decimal_to_prob,
    devig_multiplicative,
    fair_probs,
    vig_pct,
)


def test_american_favorite_odds():
    # -110 -> 110/210
    assert math.isclose(american_to_prob(-110), 110 / 210, rel_tol=1e-9)


def test_american_underdog_odds():
    # +150 -> 100/250
    assert math.isclose(american_to_prob(150), 100 / 250, rel_tol=1e-9)


def test_american_zero_is_invalid():
    try:
        american_to_prob(0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_decimal_odds():
    assert math.isclose(decimal_to_prob(2.0), 0.5, rel_tol=1e-9)


def test_devig_two_way_sums_to_one():
    raw = [american_to_prob(-110), american_to_prob(-110)]
    devigged = devig_multiplicative(raw)
    assert math.isclose(sum(devigged), 1.0, rel_tol=1e-9)
    assert math.isclose(devigged[0], 0.5, rel_tol=1e-9)  # symmetric line -> 50/50 fair


def test_vig_pct_is_positive_for_a_real_line():
    raw = [american_to_prob(-110), american_to_prob(-110)]
    assert vig_pct(raw) > 0  # -110/-110 implies ~4.76% vig


def test_aggregate_fair_probs_averages_across_books():
    bookmakers = [
        {"key": "bookA", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Team1", "price": -120}, {"name": "Team2", "price": 100},
        ]}]},
        {"key": "bookB", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Team1", "price": -110}, {"name": "Team2", "price": -110},
        ]}]},
    ]
    avg, per_book = aggregate_fair_probs(bookmakers)
    assert set(per_book.keys()) == {"bookA", "bookB"}
    assert math.isclose(sum(avg.values()), 1.0, rel_tol=1e-6)
    # average should sit between the two books' individual fair probs for Team1
    assert min(per_book["bookA"]["Team1"], per_book["bookB"]["Team1"]) <= avg["Team1"]
    assert avg["Team1"] <= max(per_book["bookA"]["Team1"], per_book["bookB"]["Team1"])


def test_aggregate_fair_probs_skips_books_without_h2h():
    bookmakers = [
        {"key": "bookA", "markets": [{"key": "spreads", "outcomes": []}]},
        {"key": "bookB", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Team1", "price": -110}, {"name": "Team2", "price": -110},
        ]}]},
    ]
    avg, per_book = aggregate_fair_probs(bookmakers)
    assert set(per_book.keys()) == {"bookB"}


def test_fair_probs_from_book_lines():
    lines = [
        BookLine(bookmaker="fanduel", outcome="Cowboys", american_odds=225),
        BookLine(bookmaker="fanduel", outcome="Buccaneers", american_odds=-275),
    ]
    probs = fair_probs(lines)
    assert math.isclose(sum(probs.values()), 1.0, rel_tol=1e-9)
    assert probs["Buccaneers"] > probs["Cowboys"]  # -275 favorite should stay favored after devig
