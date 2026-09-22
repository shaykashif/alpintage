import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from run_cross_venue_scanner import _align_probs, _max_divergence  # noqa: E402


def test_aligns_differently_spelled_team_names():
    kalshi = {"Chicago": 0.635, "Philadelphia": 0.365}
    odds = {"Chicago Bears": 0.616, "Philadelphia Eagles": 0.384}
    diffs = _align_probs(kalshi, odds)
    assert set(diffs.keys()) == {"Chicago", "Philadelphia"}
    assert abs(diffs["Chicago"] - abs(0.635 - 0.616)) < 1e-9


def test_max_divergence_uses_alignment_not_exact_match():
    kalshi = {"Buffalo": 0.745, "Los Angeles C": 0.255}
    odds = {"Buffalo Bills": 0.740, "Los Angeles Chargers": 0.260}
    diff = _max_divergence(kalshi, odds)
    assert diff is not None
    assert diff < 0.02  # these are the same two teams, prices are close


def test_empty_input_returns_no_diffs():
    # _align_probs assumes the caller already confirmed (via likely_same_game
    # + Jev) that both sides are the same game -- it only decides WHICH team
    # is which, not whether they match at all. So unrelated names still get
    # a best-effort pairing; only empty input has nothing to pair.
    assert _align_probs({}, {"Kansas City Chiefs": 0.5}) == {}
    assert _align_probs({"Chicago": 0.5}, {}) == {}
    assert _max_divergence({}, {"Kansas City Chiefs": 0.5}) is None


def test_x_vs_x_state_aligns_correctly():
    # Real case caught live: a naive per-team greedy word-overlap match
    # cross-wired these (both Kalshi names reduce to the same tokens),
    # producing a fake 0.60 "divergence" out of teams that actually agreed
    # within 0.03. This must not regress.
    kalshi = {"New Mexico": 0.815, "New Mexico St.": 0.185}
    odds = {"New Mexico Lobos": 0.789, "New Mexico State Aggies": 0.211}
    diffs = _align_probs(kalshi, odds)
    assert diffs["New Mexico"] < 0.05
    assert diffs["New Mexico St."] < 0.05
