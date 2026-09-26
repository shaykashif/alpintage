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


def test_polymarket_fetch_follows_the_keyset_cursor_and_filters(monkeypatch):
    calls = []

    def ev(slug, markets):
        return {"slug": slug, "title": slug, "negRisk": True, "markets": markets}

    def m(slug, **kw):
        out = {k: v for k, v in pm(slug, **kw).items() if k != "events"}
        out["endDate"] = "2000-01-01T00:00:00Z"  # inside any horizon
        return out

    def fake_get(url, params, timeout):
        calls.append(params)
        assert url.endswith("/events/keyset") and params["exclude_tag_id"] == rs.SPORTS_TAG_ID
        if "after_cursor" not in params:
            return FakeResponse({"events": [ev("ev", [m("a"), m("sports", fee="sports_fees_v3")]), ev("ev2", [m("b")])],
                                 "next_cursor": "c1"})
        return FakeResponse({"events": [ev("ev", [m("a"), m("c", outcomes=("Lakers", "Celtics")), {**m("d"), "closed": True}]),
                                        ev("ev3", [{**m("late"), "endDate": "2999-01-01T00:00:00Z"}])],
                             "next_cursor": None})

    monkeypatch.setattr(rs.httpx, "get", fake_get)
    monkeypatch.setattr(rs, "PAGE_PACING_S", 0)
    events = rs.fetch_polymarket_events(horizon_days=4, page_size=3)

    assert [c.get("after_cursor") for c in calls] == [None, "c1"]
    slugs = sorted(x["slug"] for e in events for x in e.markets)
    assert slugs == ["a", "b"]  # deduped; sports, non-Yes/No, closed and past-horizon dropped
    a = next(x for e in events for x in e.markets if x["slug"] == "a")
    assert a["events"][0]["slug"] == "ev" and "markets" not in a["events"][0]  # the /markets shape
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
