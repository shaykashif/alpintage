"""Rebuilds paper positions and realized PnL from data/paper_fills.jsonl --
the single source of truth for every paper strategy. PaperBroker starts
fresh in memory on each script run, so anything that needs to know "what
do we hold right now" (risk exposure, stat-arb exits, the scorer, the
dashboard) replays this log instead of trusting in-process state.

Row kinds (the `event` field):
  - "fill":   a paper buy. cost_usd already includes the taker fee.
  - "sell":   a paper exit before settlement. proceeds_usd is net of fee.
  - "veto":   a refused order -- ignored here.
Settlement is not logged as a row; score_paper_fills.py applies it using
the market's result and writes it to the cached summary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "paper_fills.jsonl"

STAT_ARB_STRATEGY = "stat_arb"
_STAT_ARB_REASON_PREFIX = "cross-venue stat-arb"


@dataclass
class Position:
    ticker: str
    side: str
    strategy: str
    qty_bought: float = 0.0
    qty_sold: float = 0.0
    cost_usd: float = 0.0  # total paid for every contract bought, fees included
    realized_pnl_usd: float = 0.0  # from sells only; settlement is applied by the scorer
    first_ts: str | None = None
    last_ts: str | None = None
    meta: dict = field(default_factory=dict)  # latest entry's venue / fair / event_ticker

    @property
    def qty_open(self) -> float:
        return round(self.qty_bought - self.qty_sold, 6)

    @property
    def avg_cost(self) -> float:
        """Per-contract cost basis, fees included."""
        return self.cost_usd / self.qty_bought if self.qty_bought else 0.0

    @property
    def open_cost_usd(self) -> float:
        return self.avg_cost * self.qty_open


def load_rows(log_path: Path | str = DEFAULT_LOG_PATH) -> list[dict]:
    path = Path(log_path)
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


def strategy_of(row: dict) -> str:
    """Rows written before the `strategy` field existed only carry a free-
    text reason, so fall back to recognizing the stat-arb reason prefix."""
    if row.get("strategy"):
        return row["strategy"]
    if (row.get("reason") or "").startswith(_STAT_ARB_REASON_PREFIX):
        return STAT_ARB_STRATEGY
    return "ladder_bracket"


def venue_of(row: dict) -> str | None:
    if row.get("venue"):
        return row["venue"]
    reason = row.get("reason") or ""
    for venue in ("odds_api", "polymarket"):
        if venue in reason:
            return venue
    return None


def build_positions(rows: list[dict]) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    for row in rows:
        event = row.get("event")
        if event not in ("fill", "sell"):
            continue
        ticker = row["ticker"]
        pos = positions.get(ticker)
        if pos is None:
            pos = positions[ticker] = Position(ticker=ticker, side=row.get("side", "yes"), strategy=strategy_of(row))
            pos.first_ts = row.get("ts")
        pos.last_ts = row.get("ts")

        if event == "fill":
            pos.qty_bought += row.get("qty", 0.0)
            pos.cost_usd += row.get("cost_usd", 0.0)
            pos.meta = {
                "venue": venue_of(row),
                "fair_at_entry": row.get("fair"),
                "event_ticker": row.get("event_ticker"),
                "entry_price": row.get("price"),
            }
        else:
            qty = row.get("qty", 0.0)
            # Cost basis of what's being sold is taken BEFORE the sell
            # reduces qty_open -- avg_cost doesn't change on a sell.
            pos.realized_pnl_usd += row.get("proceeds_usd", 0.0) - pos.avg_cost * qty
            pos.qty_sold += qty
    return positions


def open_positions(rows: list[dict], exclude_tickers: set[str] | None = None) -> dict[str, Position]:
    """Positions with contracts still held. `exclude_tickers` is for markets
    already known to be settled (score_paper_fills.py's cached summary) --
    settlement isn't a log row, so without this a settled position would
    look open forever."""
    exclude = exclude_tickers or set()
    return {t: p for t, p in build_positions(rows).items() if p.qty_open > 0 and t not in exclude}
