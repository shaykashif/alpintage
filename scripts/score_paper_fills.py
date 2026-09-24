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
legs of relation arbs ("PM-<slug>") are fetched from Polymarket instead
(status from Gamma, bid/ask from the CLOB order book),
and charged Polymarket's own taker fee on exit.

Relation-arb legs bought together (same arb_group) are valued as ONE
hedged set, not as independent bets: the set is worth the larger of its
guaranteed payout at resolution (if the relation holds) and what selling
every leg now would fetch. Marking each leg at its own bid charged the
full bid-ask spread on every leg, so a hedged pair in a thin market read
as a loss even though it is locked to pay out at least its payout.

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
from kalshi_engine.relation_sources import (  # noqa: E402
    fetch_polymarket_market, fetch_polymarket_quotes, polymarket_as_kalshi_shape,
)

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
        if not pm:
            return None
        shaped = polymarket_as_kalshi_shape(pm)
        if shaped["status"] == "active":
            # Mark against the live order book, not Gamma's lagging bestBid/bestAsk.
            q = fetch_polymarket_quotes([ticker[3:]]).get(ticker)
            if q:
                shaped["yes_bid_dollars"], shaped["yes_ask_dollars"] = q.yes_bid, q.yes_ask
        return shaped
    return client.market(ticker)


def mark_price(market: dict, side: str) -> float | None:
    """What selling one contract of `side` would fetch right now."""
    if side == "yes":
        bid = market.get("yes_bid_dollars")
        return float(bid) if bid is not None else None
    ask = market.get("yes_ask_dollars")  # a NO bid is the complement of the YES ask
    return round(1 - float(ask), 4) if ask is not None else None


# Guaranteed payout per set when a fill row predates payout_per_set: every
# two-leg relation pays >= $1; a mutually-exclusive event set of n NOs pays n - 1.
RELATION_PAYOUT = {"a_implies_b": 1.0, "b_implies_a": 1.0, "mutually_exclusive": 1.0, "exhaustive": 1.0}


def exit_fee(market: dict, qty: float, price: float) -> float:
    """Taker fee to sell at `price`: Polymarket's schedule when the market
    carries one (polymarket_as_kalshi_shape), else Kalshi's formula."""
    if "fee_rate" in market:
        return round(market["fee_rate"] * qty * (price * (1 - price)) ** market.get("fee_exponent", 1.0), 4)
    return float(taker_fee(qty, price))


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
        "arb_group": pos.meta.get("arb_group"),
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
        liquidation = mark * pos.qty_open - exit_fee(market, pos.qty_open, mark)
    else:
        liquidation = 0.0  # no bid: worth nothing until settlement
    row["liquidation"] = round(liquidation, 4)
    row["open_cost"] = round(pos.open_cost_usd, 4)
    row["unrealized_pnl"] = round(liquidation - pos.open_cost_usd, 4)
    return row


def payout_per_set(pos: ledger.Position, n_legs: int) -> float | None:
    stored = pos.meta.get("payout_per_set")
    if stored is not None:
        return float(stored)
    relation = pos.meta.get("relation")
    if relation == "me_event_overround":
        return float(n_legs - 1)
    return RELATION_PAYOUT.get(relation)


def mark_arb_sets(rows: list[dict], positions: dict[str, ledger.Position]) -> list[dict]:
    """Re-value open relation-arb legs as hedged sets (see module doc).

    A set qualifies while every leg is still held in full -- a leg partly or
    wholly sold breaks the hedge, and those legs keep their own per-leg mark.
    Legs that already settled count toward the guarantee: whatever they paid
    is subtracted from what the still-open legs must pay. Each open leg's
    `unrealized_pnl` becomes its cost-weighted share of the set's, so book
    totals stay a plain sum over rows; the per-leg figure is kept as
    `leg_unrealized_pnl`. Returns one summary row per set."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("arb_group"):
            groups.setdefault(r["arb_group"], []).append(r)

    sets = []
    for group, legs in groups.items():
        open_legs = [r for r in legs if r["status"] in ("open", "unknown")]
        if not open_legs or any(r["status"] == "exited" or r["qty_open"] != r["qty_bought"] for r in legs):
            continue
        qty = legs[0]["qty_bought"]
        per_set = payout_per_set(positions[legs[0]["ticker"]], len(legs))
        if per_set is None or any(r["qty_bought"] != qty for r in legs):
            continue

        settled_paid = sum(r.get("payout", 0.0) for r in legs if r["status"] == "settled")
        guaranteed = max(0.0, per_set * qty - settled_paid)
        open_cost = sum(positions[r["ticker"]].open_cost_usd for r in open_legs)
        # A leg with no fetched market has no sale value; the guarantee
        # doesn't depend on prices, so it still holds.
        priced = all(r["status"] == "open" for r in open_legs)
        liquidation = sum(r["liquidation"] for r in open_legs) if priced else None
        value = max(guaranteed, liquidation) if liquidation is not None else guaranteed
        unrealized = value - open_cost

        for r in open_legs:
            share = positions[r["ticker"]].open_cost_usd / open_cost if open_cost else 1 / len(open_legs)
            r["leg_unrealized_pnl"] = r["unrealized_pnl"]
            r["unrealized_pnl"] = round(unrealized * share, 4)
            if r["status"] == "unknown":
                r["status"] = "open"  # valued through its set

        sets.append({
            "arb_group": group,
            "relation": positions[legs[0]["ticker"]].meta.get("relation"),
            "tickers": [r["ticker"] for r in legs],
            "qty": qty,
            "open_cost": round(open_cost, 4),
            "guaranteed_payout": round(guaranteed, 4),
            "liquidation": round(liquidation, 4) if liquidation is not None else None,
            "value": round(value, 4),
            "unrealized_pnl": round(unrealized, 4),
            # Lost if the relation turns out wrong and every open leg expires worthless.
            "at_risk": round(open_cost, 4),
            "opened_at": min((r["opened_at"] for r in legs if r["opened_at"]), default=None),
        })
    return sets


def summarize(rows: list[dict], generated_at: str, arb_sets: list[dict] | None = None) -> dict:
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
        "arb_sets": arb_sets or [],
        "relation_at_risk": round(sum(x["at_risk"] for x in arb_sets or []), 2),
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
        rows.append(score_position(pos, market))

    arb_sets = mark_arb_sets(rows, positions)
    for row in rows:
        pnl = row["realized_pnl"] + row["unrealized_pnl"]
        print(f"  [{row['strategy']}] {row['ticker']}: {row['status']} qty_open={row['qty_open']:g} cost=${row['cost']:.2f} pnl=${pnl:+.2f}")
    for x in arb_sets:
        print(f"  set {x['arb_group']}: value ${x['value']:.2f} (guaranteed ${x['guaranteed_payout']:.2f}) pnl=${x['unrealized_pnl']:+.2f}")

    summary = summarize(rows, generated_at, arb_sets)
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
            # Per-strategy curve components, so the dashboard can chart one
            # strategy on its own (realized vs. total, capital at risk).
            "by_strategy_detail": {
                s: {k: t[k] for k in ("total_pnl", "realized_pnl", "unrealized_pnl", "open_cost")}
                for s, t in summary["by_strategy"].items()
            },
        }) + "\n")


if __name__ == "__main__":
    main()
