import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import semantic  # noqa: E402
from kalshi_engine.relations import Contract  # noqa: E402


class StubModel:
    """Maps a few known phrases to fixed vectors instead of loading a real model."""
    VEC = {"inflation": [1, 0, 0], "cpi": [0.95, 0.31, 0], "netflix": [0, 1, 0], "weather": [0, 0, 1]}

    def embed(self, texts):
        for t in texts:
            yield np.array(next(v for k, v in self.VEC.items() if k in t.lower()), dtype=float)


def C(ticker, title, venue):
    return Contract(venue=venue, ticker=ticker, event_id="", category=None, title=title, context="",
                    close_time=None, yes_bid=None, yes_ask=None)


def test_cross_venue_pairs_by_meaning(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "_model", StubModel())
    ms = [C("PM-infl", "Will inflation exceed 3.1% in August?", "polymarket"),
          C("KXCPI-1", "CPI above 3.1%? -- Above 3.1%", "kalshi"),
          C("KXNFLX-1", "Top Netflix show?", "kalshi"),
          C("KXWX-1", "Weather in NYC?", "kalshi")]
    pairs = semantic.cross_venue_pairs(ms, k=2, min_sim=0.9, cache_path=tmp_path / "e.jsonl")
    assert [(a.ticker, b.ticker) for a, b, _ in pairs] == [("KXCPI-1", "PM-infl")]  # lower ticker first
    assert pairs[0][2] > 0.9
    # Second call reuses the on-disk cache: no model needed.
    monkeypatch.setattr(semantic, "_model", None)
    monkeypatch.setattr(semantic, "TextEmbedding", None, raising=False)
    again = semantic.cross_venue_pairs(ms, k=2, min_sim=0.9, cache_path=tmp_path / "e.jsonl")
    assert [(a.ticker, b.ticker) for a, b, _ in again] == [("KXCPI-1", "PM-infl")]
