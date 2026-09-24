import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run_cross_venue_scanner as rcvs  # noqa: E402
from kalshi_engine.event_match import PairingCandidate  # noqa: E402


@dataclass
class FakeGame:
    team_probs: dict
    rules_text: str = "If New Mexico wins the game originally scheduled for Sep 26, 2026, then..."


def test_aligns_differently_spelled_team_names():
    kalshi = {"Chicago": 0.635, "Philadelphia": 0.365}
    odds = {"Chicago Bears": 0.616, "Philadelphia Eagles": 0.384}
    assert dict(rcvs.best_team_assignment(kalshi, odds)) == {
        "Chicago": "Chicago Bears", "Philadelphia": "Philadelphia Eagles",
    }


def test_max_divergence_uses_the_team_map():
    kalshi = {"Buffalo": 0.745, "Los Angeles C": 0.255}
    odds = {"Buffalo Bills": 0.740, "Los Angeles Chargers": 0.260}
    team_map = {"Buffalo": "Buffalo Bills", "Los Angeles C": "Los Angeles Chargers"}
    assert rcvs._max_divergence(kalshi, odds, team_map) < 0.02


def test_no_verified_map_means_no_divergence():
    kalshi = {"Buffalo": 0.745, "Los Angeles C": 0.255}
    odds = {"Buffalo Bills": 0.740, "Los Angeles Chargers": 0.260}
    assert rcvs._max_divergence(kalshi, odds, None) is None


def test_empty_input_has_nothing_to_pair():
    assert rcvs.best_team_assignment({}, {"Kansas City Chiefs": 0.5}) == []
    assert rcvs.best_team_assignment({"Chicago": 0.5}, {}) == []


def test_x_vs_x_state_proposes_correct_pairing_first():
    # Real case caught live: a naive per-team word-overlap match cross-wired
    # these, producing a fake 0.60 "divergence". Similarity must still
    # PROPOSE the right pairing first; Jev now confirms it.
    kalshi = {"New Mexico": 0.815, "New Mexico St.": 0.185}
    odds = {"New Mexico Lobos": 0.789, "New Mexico State Aggies": 0.211}
    ranked = rcvs.ranked_team_assignments(kalshi, odds)
    assert dict(ranked[0]) == {"New Mexico": "New Mexico Lobos", "New Mexico St.": "New Mexico State Aggies"}
    assert dict(ranked[1]) == {"New Mexico": "New Mexico State Aggies", "New Mexico St.": "New Mexico Lobos"}


# --- Jev verifies the team pairing ---

def _jev_confirms(good_pairing: dict | None, calls: list):
    def fake(a_label, a_text, b_label, b_text, pairs):
        calls.append(dict(pairs))
        ok = good_pairing is not None and dict(pairs) == good_pairing
        return PairingCandidate(pairs=pairs, jev_prob=0.95 if ok else 0.05, jev_route="test", confirmed=ok)
    return fake


def test_jev_confirms_best_guess_in_one_call(monkeypatch):
    calls = []
    kg = FakeGame({"New Mexico": 0.815, "New Mexico St.": 0.185})
    odds = {"New Mexico Lobos": 0.789, "New Mexico State Aggies": 0.211}
    right = {"New Mexico": "New Mexico Lobos", "New Mexico St.": "New Mexico State Aggies"}
    monkeypatch.setattr(rcvs, "confirm_team_pairing", _jev_confirms(right, calls))

    team_map, prob = rcvs.verified_team_map(kg, "Odds API", "Lobos vs Aggies", odds)

    assert team_map == right and prob == 0.95
    assert len(calls) == 1


def test_jev_rejecting_best_guess_falls_back_to_the_swap(monkeypatch):
    # Similarity gets it wrong (names chosen so the wrong pairing scores
    # higher); Jev rejects it and confirms the alternative instead.
    calls = []
    kg = FakeGame({"Miami": 0.6, "Miami (OH)": 0.4})
    venue = {"Miami RedHawks": 0.62, "Miami Hurricanes": 0.38}
    right = {"Miami": "Miami Hurricanes", "Miami (OH)": "Miami RedHawks"}
    monkeypatch.setattr(rcvs, "confirm_team_pairing", _jev_confirms(right, calls))

    team_map, _ = rcvs.verified_team_map(kg, "Polymarket", "Miami game", venue)

    assert team_map == right
    assert len(calls) == 2


def test_venue_dropped_when_jev_confirms_no_pairing(monkeypatch):
    calls = []
    kg = FakeGame({"Chicago": 0.6, "Philadelphia": 0.4})
    odds = {"Chicago Bears": 0.6, "Philadelphia Eagles": 0.4}
    monkeypatch.setattr(rcvs, "confirm_team_pairing", _jev_confirms(None, calls))

    team_map, prob = rcvs.verified_team_map(kg, "Odds API", "Bears @ Eagles", odds)

    assert team_map is None and prob == 0.05
    assert len(calls) == rcvs.MAX_PAIRING_CHECKS


def test_reference_fairs_ignores_venues_without_a_verified_map():
    kg = FakeGame({"Chicago": 0.6, "Philadelphia": 0.4})
    row = {
        "odds_api_avg_probs": {"Chicago Bears": 0.7, "Philadelphia Eagles": 0.3},
        "odds_api_team_map": None,
        "polymarket_probs": {"Bears": 0.65, "Eagles": 0.35},
        "polymarket_team_map": {"Chicago": "Bears", "Philadelphia": "Eagles"},
    }
    assert rcvs._reference_fairs(kg, row) == {"polymarket": {"Chicago": 0.65, "Philadelphia": 0.35}}
