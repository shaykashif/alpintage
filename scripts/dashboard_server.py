"""Serves the dashboard: GET / renders the page, GET /api/summary returns
the current data as JSON (dashboard_data.py, read-only, no live API calls).

Runs open with no authentication by default, per the project owner's choice
-- no secrets or real money are exposed by this data, but anyone who has the
server's IP can view it. To add basic auth later, see the comment in main().

Usage (local):
    uv run python scripts/dashboard_server.py
    uv run python scripts/dashboard_server.py --port 8080 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flask import Flask, jsonify, send_from_directory  # noqa: E402

from kalshi_engine.dashboard_data import full_summary  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    # Always revalidate, so a deploy (git pull) shows up on the next load.
    return send_from_directory(DASHBOARD_DIR, "index.html", max_age=0)


@app.route("/assets/<path:filename>")
def assets(filename):
    # Self-hosted static files (fonts). send_from_directory refuses paths
    # that escape the directory. assets/fonts/ is gitignored -- see
    # .gitignore -- so a fresh checkout simply falls back to the free
    # web fonts the page also declares.
    # Fonts never change, so cache them for a day; CSS/JS revalidate on
    # every load (ETag, so unchanged files are a cheap 304) -- a long cache
    # on code served stale pages after edits during development.
    max_age = 86400 if filename.startswith("fonts/") else 0
    return send_from_directory(DASHBOARD_DIR / "assets", filename, max_age=max_age)


@app.route("/api/summary")
def api_summary():
    return jsonify(full_summary())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to accept connections from outside the VM")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    # To add basic auth: wrap the two routes above with a before_request
    # check against an env var (e.g. DASHBOARD_PASSWORD), and return a 401
    # with a WWW-Authenticate header if it doesn't match. Left out per the
    # project owner's choice to leave this open.
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
