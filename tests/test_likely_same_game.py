from kalshi_engine.event_match import likely_same_game


def test_shared_team_name_passes():
    a = "If Philadelphia wins the Philadelphia vs Chicago Pro Football game originally scheduled for Sep 28, 2026, then the market resolves to Yes."
    b = "Philadelphia Eagles @ Chicago Bears, kickoff 2026-09-28T17:00:00Z"
    assert likely_same_game(a, b)


def test_unrelated_games_are_rejected():
    a = "If Philadelphia wins the Philadelphia vs Chicago Pro Football game originally scheduled for Sep 28, 2026, then the market resolves to Yes."
    b = "Kansas City Chiefs @ Miami Dolphins, kickoff 2026-09-28T17:00:00Z"
    assert not likely_same_game(a, b)


def test_ignores_generic_stopwords():
    a = "Pro Football Game"
    b = "College Basketball Game"
    assert not likely_same_game(a, b)  # only shared words are stopwords -- not a real match
