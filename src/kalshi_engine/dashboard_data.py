"""Reads the project's existing JSONL logs (never calls a live API) and
produces the summary the dashboard renders. Deliberately fast and read-only
so the web server stays cheap to run on a small instance regardless of
visitor traffic -- any slow, API-calling work (checking settlement, scoring
predictions) happens in the periodic scripts (run_loop.py), which write
their results to these same log files or the cached PnL summary this reads.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _brier(rows: list[dict], prob_key: str) -> float | None:
    if not rows:
        return None
    return sum((r[prob_key] - r["outcome"]) ** 2 for r in rows) / len(rows)


def predictions_summary(min_scored_for_trend: int = 10) -> dict:
    rows = _load_jsonl(DATA_DIR / "predictions.jsonl")
    scored = [r for r in rows if r.get("outcome") is not None]
    real = [r for r in rows if r.get("jev_route") == "typesafe"]

    scored_sorted = sorted(scored, key=lambda r: r.get("scored_at") or "")
    trend = []
    if len(scored_sorted) >= min_scored_for_trend:
        for i in range(min_scored_for_trend, len(scored_sorted) + 1):
            window = scored_sorted[:i]
            trend.append({
                "n": i,
                "jev_brier": round(_brier(window, "jev_prob"), 4),
                "market_brier": round(_brier(window, "market_prob"), 4),
            })

    return {
        "total_logged": len(rows),
        "total_real_jev_calls": len(real),
        "total_scored": len(scored),
        "jev_brier": round(_brier(scored, "jev_prob"), 4) if scored else None,
        "market_brier": round(_brier(scored, "market_prob"), 4) if scored else None,
        "brier_trend": trend,
        "recent": [
            {
                "ticker": r["ticker"],
                "market_prob": r["market_prob"],
                "jev_prob": r["jev_prob"],
                "outcome": r.get("outcome"),
                "logged_at": r["logged_at"],
            }
            for r in sorted(rows, key=lambda r: r["logged_at"], reverse=True)[:15]
        ],
    }


def cross_venue_summary(top_n: int = 15) -> dict:
    rows = _load_jsonl(DATA_DIR / "cross_venue.jsonl")

    by_league: dict[str, int] = {}
    diffs = []
    for r in rows:
        by_league[r.get("league", "?")] = by_league.get(r.get("league", "?"), 0) + 1
        d = max(r.get("max_diff_vs_odds_api") or 0, r.get("max_diff_vs_polymarket") or 0)
        if d:
            diffs.append(d)

    bins = [0.0, 0.01, 0.02, 0.03, 0.05, 0.10, 1.0]
    histogram = [0] * (len(bins) - 1)
    for d in diffs:
        for i in range(len(bins) - 1):
            if bins[i] <= d < bins[i + 1]:
                histogram[i] += 1
                break

    ranked = sorted(
        rows,
        key=lambda r: max(r.get("max_diff_vs_odds_api") or 0, r.get("max_diff_vs_polymarket") or 0),
        reverse=True,
    )[:top_n]

    return {
        "total_logged": len(rows),
        "by_league": by_league,
        "avg_diff": round(sum(diffs) / len(diffs), 4) if diffs else None,
        "max_diff": round(max(diffs), 4) if diffs else None,
        "histogram_bins": bins,
        "histogram_counts": histogram,
        "top_divergences": [
            {
                "league": r.get("league"),
                "kalshi_event_ticker": r.get("kalshi_event_ticker"),
                "kalshi_probs": r.get("kalshi_probs"),
                "diff": round(max(r.get("max_diff_vs_odds_api") or 0, r.get("max_diff_vs_polymarket") or 0), 4),
                "logged_at": r.get("logged_at"),
            }
            for r in ranked
        ],
    }


def paper_trading_summary() -> dict:
    """Fills/vetoes come straight from the log (fast). Realized PnL comes
    from a cached file that score_paper_fills.py writes -- see its
    --write-summary flag -- since computing it live means calling Kalshi
    per open position, which does not belong in a web request path."""
    rows = _load_jsonl(DATA_DIR / "paper_fills.jsonl")
    fills = [r for r in rows if r.get("event") == "fill"]
    vetoes = [r for r in rows if r.get("event") == "veto"]

    cached_pnl_path = DATA_DIR / "paper_pnl_summary.json"
    pnl_summary = None
    if cached_pnl_path.exists():
        try:
            pnl_summary = json.loads(cached_pnl_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pnl_summary = None

    return {
        "total_fills": len(fills),
        "total_vetoes": len(vetoes),
        "recent_vetoes": [r.get("reason") for r in vetoes[-5:]],
        "pnl_summary": pnl_summary,
    }


def loop_health() -> dict:
    log_path = DATA_DIR / "loop.log"
    if not log_path.exists():
        return {"running": False, "last_line_at": None, "recent_lines": []}

    text = log_path.read_text(encoding="utf-8", errors="ignore")
    lines = [l for l in text.splitlines() if l.strip()]
    last_timestamp = None
    for line in reversed(lines):
        if line.startswith("["):
            try:
                last_timestamp = line[1:line.index("]")]
                break
            except ValueError:
                continue

    stale = True
    if last_timestamp:
        try:
            last_dt = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
            stale = (datetime.now(timezone.utc) - last_dt).total_seconds() > 3600 * 2
        except ValueError:
            pass

    return {
        "last_line_at": last_timestamp,
        "stale": stale,
        "recent_lines": lines[-30:],
    }


def full_summary() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "predictions": predictions_summary(),
        "cross_venue": cross_venue_summary(),
        "paper_trading": paper_trading_summary(),
        "loop_health": loop_health(),
    }
