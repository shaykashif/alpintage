"""Score the paper book in data/paper_fills.jsonl: realized PnL from exits
(sell rows) and settled markets, plus mark-to-market PnL on everything
still open (valued at the bid a sell would actually get, net of the exit
fee). Writes:

  - data/paper_pnl_summary.json: the latest per-position snapshot, cached
    so the dashboard (dashboard_data.py) never makes a live API call.
  - data/paper_equity.jsonl: one appended row per run -- the equity curve
    the dashboard plots. Run on every loop cycle, so it's a time series.

Positions fully closed by an exit need no API call; only still-held
tickers are fetched (paced, to stay clear of Kalshi's 429s). Polymarket
legs of relation arbs ("PM-<slug>") are fetched from Polymarket instead;
their mark uses Kalshi's fee formula for the exit fee, a close-enough
approximation for a paper mark.

Usage:
    uv run python scripts/score_paper_fills.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import ledger  # noqa: E402
from kalshi_engine.fees import taker_fee  # noqa: E402
from kalshi_engine.kalshi_public import PublicClient  # noqa: E402
from kalshi_engine.relation_sources import fetch_polymarket_market, polymarket_as_kalshi_shape  # noqa: E402

FILLS_PATH = ledger.DEFAULT_LOG_PATH
SUMMARY_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_pnl_summary.json"
EQUITY_PATH = Path(__file__).resolve().parent.parent / "data" / "paper_equity.jsonl"

STARTING_CASH_USD = 1000.0
REQUEST_PACING_S = 0.15


def fetch_market(client: PublicClient, ticker: str) -> dict | None:
    """Kalshi market, or a Polymarket leg of a relation arb ("PM-<slug>")
    mapped onto the Kalshi fields scoring reads."""
    if ticker.startswith("PM-"):
        pm = fetch_polymarket_market(ticker[3:])
        return polymarket_as_kalshi_shape(pm) if pm else None
    return client.market(ticker)


def mark_price(market: dict, side: str) -> float | None:
    """What selling one contract of `side` would fetch right now."""
    if side == "yes":
        bid = market.get("yes_bid_dollars")
        return float(bid) if bid is not None else None
    ask = market.get("yes_ask_dollars")  # a NO bid is the complement of the YES ask
    return round(1 - float(ask), 4) if ask is not None else None


def score_position(pos: ledger.Position, market: dict | None) -> dict:
    """One position's row for the summary. `market` is None when the
    position was fully exited (no fetch needed) or the fetch failed."""
    row = {
        "ticker": pos.ticker, "strategy": pos.strategy, "side": pos.side,
        "venue": pos.meta.get("venue"),
        "qty_bought": pos.qty_bought, "qty_open": pos.qty_open,
        "cost": round(pos.cost_usd, 4), "avg_cost": round(pos.avg_cost, 4),
        "realized_pnl": round(pos.realized_pnl_usd, 4), "unrealized_pnl": 0.0,
        "opened_at": pos.first_ts,
    }
    if pos.qty_open <= 0:
        row["status"] = "exited"
        return row
    if market is None:
        row["status"] = "unknown"
        return row

    if market.get("status") in ("settled", "finalized"):
        result = market.get("result")
        payout = pos.qty_open * (1.0 if result == pos.side else 0.0)
        row.update({
            "status": "settled", "result": result, "payout": round(payout, 4),
            "realized_pnl": round(pos.realized_pnl_usd + payout - pos.open_cost_usd, 4),
        })
        return row

    mark = mark_price(market, pos.side)
    row["status"] = "open"
    row["mark"] = mark
    if mark is not None and mark > 0:
        liquidation = mark * pos.qty_open - float(taker_fee(pos.qty_open, mark))
        row["unrealized_pnl"] = round(liquidation - pos.open_cost_usd, 4)
    else:
        row["unrealized_pnl"] = round(-pos.open_cost_usd, 4)  # no bid: worth nothing until settlement
    return row


def summarize(rows: list[dict], generated_at: str) -> dict:
    """Aggregate per-position rows into book totals and a per-strategy split."""
    def totals(subset: list[dict]) -> dict:
        realized = sum(r["realized_pnl"] for r in subset)
        unrealized = sum(r["unrealized_pnl"] for r in subset)
        closed = [r for r in subset if r["status"] in ("exited", "settled")]
        return {
            "positions": len(subset),
            "open_count": sum(1 for r in subset if r["status"] == "open"),
            "closed_count": len(closed),
            "wins": sum(1 for r in closed if r["realized_pnl"] > 0),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(realized + unrealized, 2),
            "open_cost": round(sum(r["avg_cost"] * r["qty_open"] for r in subset if r["status"] == "open"), 2),
        }

    book = totals(rows)
    settled = [r for r in rows if r["status"] == "settled"]
    return {
        "generated_at": generated_at,
        **book,
        # Older fields the dashboard/scanner already read -- kept stable.
        "settled_count": len(settled),
        "total_cost": round(sum(r["cost"] for r in settled), 2),
        "total_payout": round(sum(r.get("payout", 0.0) for r in settled), 2),
        "net_pnl": book["realized_pnl"],
        "by_strategy": {s: totals([r for r in rows if r["strategy"] == s]) for s in sorted({r["strategy"] for r in rows})},
        "positions": rows,
    }


def main() -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    positions = ledger.build_positions(ledger.load_rows(FILLS_PATH))
    if not positions:
        print(f"no paper fills yet at {FILLS_PATH}")
    client = PublicClient() if any(p.qty_open > 0 for p in positions.values()) else None

    rows = []
    for ticker, pos in positions.items():
        market = None
        if pos.qty_open > 0:
            try:
                market = fetch_market(client, ticker)
            except Exception as exc:  # noqa: BLE001
                print(f"  {ticker}: could not fetch ({exc})")
            time.sleep(REQUEST_PACING_S)
        row = score_position(pos, market)
        rows.append(row)
        pnl = row["realized_pnl"] + row["unrealized_pnl"]
        print(f"  [{row['strategy']}] {ticker}: {row['status']} qty_open={row['qty_open']:g} cost=${row['cost']:.2f} pnl=${pnl:+.2f}")

    summary = summarize(rows, generated_at)
    print(
        f"\n{summary['open_count']} open, {summary['closed_count']} closed "
        f"({summary['wins']} winners) -- realized ${summary['realized_pnl']:+.2f}, "
        f"unrealized ${summary['unrealized_pnl']:+.2f}, total ${summary['total_pnl']:+.2f}"
    )
    for strategy, t in summary["by_strategy"].items():
        print(f"  {strategy}: total ${t['total_pnl']:+.2f} over {t['positions']} position(s)")

    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with EQUITY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": generated_at,
            "equity": round(STARTING_CASH_USD + summary["total_pnl"], 2),
            **{k: summary[k] for k in ("realized_pnl", "unrealized_pnl", "total_pnl", "open_cost", "open_count", "closed_count")},
            "by_strategy": {s: t["total_pnl"] for s, t in summary["by_strategy"].items()},
        }) + "\n")


if __name__ == "__main__":
    main()
