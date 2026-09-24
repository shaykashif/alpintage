"""A paper-only broker: simulates fills in-process, never calls any Kalshi
trading endpoint. Kalshi order placement needs RSA-signed authenticated
requests, which this project hasn't built -- this exists so the scanner and
risk gate can be exercised end to end without that, and without risking real
money before there's evidence any of this has edge.

Assumes a fill at the quoted top-of-book price for 1 contract per leg, since
GET /markets gives no depth. A real fill could be worse (slippage) or simply
not happen (the quote moves before you get there) -- this is optimistic by
construction, not a promise of what a live order would do.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ledger
from .fees import taker_fee
from .risk import RiskLimits, RiskState, RiskVeto, check_order

DEFAULT_LOG_PATH = ledger.DEFAULT_LOG_PATH


@dataclass
class Fill:
    ticker: str
    side: str  # "yes" | "no"
    price: float
    qty: float
    fee_usd: float
    cost_usd: float
    ts: str


class PaperBroker:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        cash_usd: float = 1000.0,
        log_path: Path | str = DEFAULT_LOG_PATH,
    ):
        self.limits = limits or RiskLimits()
        self.cash_usd = cash_usd
        self.realized_pnl_today_usd = 0.0
        self.positions: dict[str, dict] = {}  # ticker -> {"side", "qty", "avg_price"}
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_ledger(
        cls,
        limits: RiskLimits | None = None,
        starting_cash_usd: float = 1000.0,
        log_path: Path | str = DEFAULT_LOG_PATH,
        settled: dict[str, float] | None = None,
    ) -> "PaperBroker":
        """A broker whose cash, open positions and today's realized PnL are
        replayed from the log, so risk limits see what's actually held
        across every past run -- not a fresh $1000 every invocation.
        `settled` maps ticker -> settlement payout (from the scorer's cached
        summary); those positions are closed out with that payout."""
        settled = settled or {}
        broker = cls(limits=limits, cash_usd=starting_cash_usd, log_path=log_path)
        rows = ledger.load_rows(log_path)
        today = datetime.now(timezone.utc).date().isoformat()
        for row in rows:
            if row.get("event") == "fill":
                broker.cash_usd -= row.get("cost_usd", 0.0)
            elif row.get("event") == "sell":
                broker.cash_usd += row.get("proceeds_usd", 0.0)
        for ticker, pos in ledger.build_positions(rows).items():
            if (pos.last_ts or "").startswith(today):
                broker.realized_pnl_today_usd += pos.realized_pnl_usd
            if ticker in settled:
                broker.cash_usd += settled[ticker]
                continue
            if pos.qty_open > 0:
                broker.positions[ticker] = {
                    "side": pos.side, "qty": pos.qty_open,
                    "avg_price": pos.avg_cost, "avg_cost": pos.avg_cost,
                }
        return broker

    def _position_usd_by_ticker(self) -> dict[str, float]:
        return {t: p["qty"] * p["avg_price"] for t, p in self.positions.items()}

    def _total_exposure_usd(self) -> float:
        return sum(self._position_usd_by_ticker().values())

    def buy(self, ticker: str, side: str, price: float, qty: float = 1.0, reason: str = "", **tags) -> Fill | None:
        """Simulate buying `qty` contracts of `side` ("yes" or "no") on
        `ticker` at `price`. Returns the Fill, or None (and logs why) if the
        risk gate vetoed it or cash was insufficient. Extra keyword `tags`
        (e.g. strategy, venue, fair, event_ticker) are logged verbatim on
        the fill row so ledger.py can reconstruct why it was opened."""
        fee = taker_fee(qty, price)
        notional = price * qty
        cost = notional + float(fee)

        state = RiskState(
            position_usd_by_ticker=self._position_usd_by_ticker(),
            total_exposure_usd=self._total_exposure_usd(),
            realized_pnl_today_usd=self.realized_pnl_today_usd,
        )
        try:
            check_order(self.limits, state, ticker, notional)
        except RiskVeto as veto:
            self._log({"event": "veto", "ticker": ticker, "side": side, "reason": str(veto)})
            return None

        if cost > self.cash_usd:
            self._log({"event": "veto", "ticker": ticker, "side": side, "reason": "insufficient paper cash"})
            return None

        self.cash_usd -= cost
        pos = self.positions.setdefault(ticker, {"side": side, "qty": 0.0, "avg_price": 0.0, "avg_cost": 0.0})
        total_qty = pos["qty"] + qty
        pos["avg_price"] = (pos["avg_price"] * pos["qty"] + price * qty) / total_qty
        # Fee-inclusive basis, what realized PnL on an exit is measured against.
        pos["avg_cost"] = (pos.get("avg_cost", pos["avg_price"]) * pos["qty"] + cost) / total_qty
        pos["qty"] = total_qty
        pos["side"] = side

        fill = Fill(
            ticker=ticker, side=side, price=price, qty=qty,
            fee_usd=float(fee), cost_usd=cost,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        self._log({"event": "fill", "reason": reason, **tags, **fill.__dict__})
        return fill

    def sell(self, ticker: str, price: float, qty: float | None = None, reason: str = "", **tags) -> Fill | None:
        """Simulate selling (closing) `qty` contracts of an open position at
        `price` -- the bid on the held side. Defaults to the whole position.
        Exits are never risk-vetoed (reducing exposure is always allowed),
        but the kill switch still applies, same as for buys. Returns None
        if there's nothing to sell."""
        pos = self.positions.get(ticker)
        if pos is None or pos["qty"] <= 0:
            return None
        if self.limits.kill_switch_path.exists():
            self._log({"event": "veto", "ticker": ticker, "side": pos["side"], "reason": "kill switch active (exit)"})
            return None
        qty = min(qty or pos["qty"], pos["qty"])
        fee = float(taker_fee(qty, price))
        proceeds = price * qty - fee
        pnl = proceeds - pos.get("avg_cost", pos["avg_price"]) * qty

        self.cash_usd += proceeds
        self.realized_pnl_today_usd += pnl
        pos["qty"] = round(pos["qty"] - qty, 6)
        if pos["qty"] <= 0:
            del self.positions[ticker]

        fill = Fill(
            ticker=ticker, side=pos["side"], price=price, qty=qty,
            fee_usd=fee, cost_usd=0.0,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        row = {"event": "sell", "reason": reason, **tags, **fill.__dict__, "proceeds_usd": round(proceeds, 4), "pnl_usd": round(pnl, 4)}
        del row["cost_usd"]
        self._log(row)
        return fill

    def _log(self, row: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
