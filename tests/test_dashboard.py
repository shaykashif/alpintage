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
    assert b"Kalshi Engine Dashboard" in resp.data


def test_api_summary_returns_expected_shape():
    client = app.test_client()
    resp = client.get("/api/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data.keys()) == {"generated_at", "predictions", "cross_venue", "paper_trading", "loop_health"}


def test_full_summary_does_not_crash_on_real_data():
    # Exercises the actual read-only aggregation against whatever's in data/
    # right now -- the important property is that it never raises, since
    # this runs on every dashboard page load.
    summary = full_summary()
    assert "predictions" in summary
    assert "cross_venue" in summary
