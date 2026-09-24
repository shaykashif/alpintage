import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dashboard_server import app  # noqa: E402
from kalshi_engine.dashboard_data import full_summary  # noqa: E402


def test_index_page_loads():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<title>Pternas" in resp.data
    assert b'rel="canonical" href="https://pternas.com/"' in resp.data


def test_api_summary_returns_expected_shape():
    client = app.test_client()
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"generated_at", "predictions", "cross_venue", "paper_trading", "relations", "loop_health"}


def test_full_summary_does_not_crash_on_real_data():
    # Exercises the actual read-only aggregation against whatever's in data/
    # right now -- the important property is that it never raises, since
    # this runs on every dashboard page load.
    summary = full_summary()
    assert "predictions" in summary
    assert "cross_venue" in summary


def test_seo_and_icon_routes():
    client = app.test_client()
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and b"Sitemap: https://pternas.com/sitemap.xml" in robots.data
    # The API stays crawlable (the page renders from it) but out of the index.
    assert b"Disallow: /api/" not in robots.data
    assert client.get("/api/summary").headers["X-Robots-Tag"] == "noindex"
    www = client.get("/?a=1", headers={"Host": "www.pternas.com"})
    assert www.status_code == 301 and www.headers["Location"] == "https://pternas.com/?a=1"
    assert client.get("/no-such-page").status_code == 404
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200 and b"<loc>https://pternas.com/</loc>" in sitemap.data
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/assets/brand/favicon.svg").status_code == 200
    assert client.get("/assets/brand/og-image.png").status_code == 200
    manifest = client.get("/site.webmanifest").get_json()
    assert manifest["name"] == "Pternas" and len(manifest["icons"]) == 2


def test_api_live_reports_running_only_when_fresh(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone

    from kalshi_engine import dashboard_data

    monkeypatch.setattr(dashboard_data, "DATA_DIR", tmp_path)
    client = app.test_client()
    assert client.get("/api/live").get_json() == {"running": False, "rows": []}

    row = {"id": "A|PM-b", "status": "violation", "edge": 0.1}
    for age, running in ((1, True), (300, False)):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        (tmp_path / "relation_live.json").write_text(json.dumps({"generated_at": ts, "rows": [row]}))
        data = client.get("/api/live").get_json()
        assert data["running"] is running and data["rows"] == [row]


def test_duplicate_violations_from_scan_and_watcher_count_once(tmp_path, monkeypatch):
    import json

    from kalshi_engine import dashboard_data

    monkeypatch.setattr(dashboard_data, "DATA_DIR", tmp_path)
    legs = [{"ticker": "PM-a", "side": "no", "price": 0.27}, {"ticker": "PM-b", "side": "no", "price": 0.36}]
    rows = [
        {"kind": "mutually_exclusive", "legs": legs, "logged_at": "2026-09-24T05:00:00", "source": "scan"},
        {"kind": "mutually_exclusive", "legs": legs, "logged_at": "2026-09-24T05:00:02", "source": "watch"},
        {"kind": "mutually_exclusive", "legs": [dict(legs[0], price=0.30), legs[1]], "logged_at": "2026-09-24T05:01:00"},
    ]
    (tmp_path / "relation_arbs.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    summary = dashboard_data.relation_summary()
    assert summary["opportunities_logged"] == 2 and len(summary["recent"]) == 2
