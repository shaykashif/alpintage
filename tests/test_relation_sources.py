import json

from kalshi_engine import relation_sources as rs


class FakeResponse:
    def __init__(self, rows=None, status=200):
        self._rows, self.status_code = rows or [], status

    def json(self):
        return self._rows

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def pm(slug, event="ev", outcomes=("Yes", "No"), fee="culture_fees"):
    return {"slug": slug, "question": slug, "outcomes": json.dumps(list(outcomes)), "feeType": fee,
            "events": [{"slug": event, "title": event}], "acceptingOrders": True}


def test_polymarket_fetch_walks_windows_dedupes_and_survives_422(monkeypatch):
    calls = []

    def fake_get(url, params, timeout):
        calls.append((params["end_date_min"], params["offset"]))
        window = len({c[0] for c in calls})
        if params["offset"] > 0:
            return FakeResponse(status=422)  # the offset ceiling: next window, not failure
        if window == 1:
            return FakeResponse([pm("a"), pm("b", event="ev2"), pm("sports", fee="sports_fees_v3")])
        return FakeResponse([pm("a"), pm("c", outcomes=("Lakers", "Celtics")), pm("d")])  # "a" repeats

    monkeypatch.setattr(rs.httpx, "get", fake_get)
    monkeypatch.setattr(rs, "PAGE_PACING_S", 0)
    events = rs.fetch_polymarket_events(horizon_days=4, window_days=2, page_size=3)

    assert len({c[0] for c in calls}) == 2  # two 2-day windows over a 4-day horizon
    slugs = sorted(m["slug"] for e in events for m in e.markets)
    assert slugs == ["a", "b", "d"]  # deduped, sports and non-Yes/No dropped
    assert {e.event_id for e in events} == {"ev", "ev2"}


def test_closed_polymarket_market_is_found_on_retry(monkeypatch):
    # Gamma omits closed markets from a plain slug lookup.
    from kalshi_engine import relation_sources as rs
    calls = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    def fake_get(url, params, timeout):
        calls.append(params)
        return Resp([{"slug": "x", "closed": True, "outcomePrices": '["0", "1"]'}] if params.get("closed") else [])

    monkeypatch.setattr(rs.httpx, "get", fake_get)
    pm = rs.fetch_polymarket_market("x")
    assert pm["slug"] == "x" and [c.get("closed") for c in calls] == [None, "true"]
    assert rs.polymarket_as_kalshi_shape(pm)["result"] == "no"
