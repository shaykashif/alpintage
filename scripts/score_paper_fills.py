"""Compute realized PnL for paper trades in data/paper_fills.jsonl, once
their markets have settled. Re-fetches settlement status each run, and
writes a cached summary to data/paper_pnl_summary.json so the dashboard
(dashboard_data.py) never has to make a live API call from a web request.

Usage:
    uv run python scripts/score_paper_fills.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine.kalshi_public import PublicClient  # noqa: E402

FILLS_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_fills.jsonl"
SUMMARY_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_pnl_summary.json"


def main() -> None:
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "settled_count": 0,
        "open_count": 0,
        "total_cost": 0.0,
        "total_payout": 0.0,
        "net_pnl": 0.0,
        "positions": [],
    }

    if not FILLS_PATH.exists():
        print(f"no paper fills yet at {FILLS_PATH}")
        _write_summary(summary)
        return

    fills = []
    with FILLS_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "fill":
                fills.append(row)

    if not fills:
        print("no fills logged yet (only vetoes, or the scanner hasn't found anything)")
        _write_summary(summary)
        return

    by_ticker = defaultdict(list)
    for fill in fills:
        by_ticker[fill["ticker"]].append(fill)

    client = PublicClient()
    total_cost = total_payout = 0.0
    settled_count = open_count = 0

    print(f"{len(by_ticker)} tickers with paper fills\n")
    for ticker, ticker_fills in by_ticker.items():
        try:
            m = client.market(ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: could not fetch ({exc})")
            continue

        cost = sum(f["cost_usd"] for f in ticker_fills)
        qty = sum(f["qty"] for f in ticker_fills)
        side = ticker_fills[0]["side"]  # this scanner only ever takes one side per ticker

        if m.get("status") != "settled":
            open_count += 1
            print(f"  {ticker}: still open (cost so far ${cost:.2f})")
            summary["positions"].append({"ticker": ticker, "status": "open", "cost": round(cost, 2)})
            continue

        result = m.get("result")
        payout = qty * 1.0 if result == side else 0.0
        pnl = payout - cost
        settled_count += 1
        total_cost += cost
        total_payout += payout
        print(f"  {ticker}: side={side} result={result} cost=${cost:.2f} payout=${payout:.2f} pnl=${pnl:+.2f}")
        summary["positions"].append({
            "ticker": ticker, "status": "settled", "side": side, "result": result,
            "cost": round(cost, 2), "payout": round(payout, 2), "pnl": round(pnl, 2),
        })

    print(f"\n{settled_count} settled, {open_count} still open")
    if settled_count:
        print(f"total cost:   ${total_cost:.2f}")
        print(f"total payout: ${total_payout:.2f}")
        print(f"net paper PnL (settled only): ${total_payout - total_cost:+.2f}")

    summary.update({
        "settled_count": settled_count,
        "open_count": open_count,
        "total_cost": round(total_cost, 2),
        "total_payout": round(total_payout, 2),
        "net_pnl": round(total_payout - total_cost, 2),
    })
    _write_summary(summary)


def _write_summary(summary: dict) -> None:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
