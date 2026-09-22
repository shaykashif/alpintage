import json

from kalshi_engine import cross_venue_trust


def _write_scored(tmp_path, rows):
    path = tmp_path / "cross_venue_scored.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def test_not_trusted_below_minimum_sample(tmp_path, monkeypatch):
    rows = [{"kalshi_brier": 0.20, "odds_api_brier": 0.10}] * 5
    path = _write_scored(tmp_path, rows)
    monkeypatch.setattr(cross_venue_trust, "SCORED_PATH", path)

    trusted, reason = cross_venue_trust.venue_trust("odds_api", min_scored=30)
    assert trusted is False
    assert "need 30" in reason


def test_trusted_when_venue_beats_kalshi_on_enough_samples(tmp_path, monkeypatch):
    rows = [{"kalshi_brier": 0.25, "odds_api_brier": 0.15}] * 35
    path = _write_scored(tmp_path, rows)
    monkeypatch.setattr(cross_venue_trust, "SCORED_PATH", path)

    trusted, reason = cross_venue_trust.venue_trust("odds_api", min_scored=30)
    assert trusted is True
    assert "beats Kalshi" in reason


def test_not_trusted_when_venue_does_not_beat_kalshi(tmp_path, monkeypatch):
    rows = [{"kalshi_brier": 0.10, "odds_api_brier": 0.25}] * 35
    path = _write_scored(tmp_path, rows)
    monkeypatch.setattr(cross_venue_trust, "SCORED_PATH", path)

    trusted, reason = cross_venue_trust.venue_trust("odds_api", min_scored=30)
    assert trusted is False
    assert "does not beat" in reason


def test_missing_file_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(cross_venue_trust, "SCORED_PATH", tmp_path / "nonexistent.jsonl")
    trusted, reason = cross_venue_trust.venue_trust("polymarket")
    assert trusted is False
