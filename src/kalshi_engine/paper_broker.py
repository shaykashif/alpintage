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

from .fees import taker_fee
from .risk import RiskLimits, RiskState, RiskVeto, check_order

DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "paper_fills.jsonl"


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

    def _position_usd_by_ticker(self) -> dict[str, float]:
        return {t: p["qty"] * p["avg_price"] for t, p in self.positions.items()}

    def _total_exposure_usd(self) -> float:
        return sum(self._position_usd_by_ticker().values())

    def buy(self, ticker: str, side: str, price: float, qty: float = 1.0, reason: str = "") -> Fill | None:
        """Simulate buying `qty` contracts of `side` ("yes" or "no") on
        `ticker` at `price`. Returns the Fill, or None (and logs why) if the
        risk gate vetoed it or cash was insufficient."""
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
        pos = self.positions.setdefault(ticker, {"side": side, "qty": 0.0, "avg_price": 0.0})
        total_qty = pos["qty"] + qty
        pos["avg_price"] = (pos["avg_price"] * pos["qty"] + price * qty) / total_qty
        pos["qty"] = total_qty
        pos["side"] = side

        fill = Fill(
            ticker=ticker, side=side, price=price, qty=qty,
            fee_usd=float(fee), cost_usd=cost,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        self._log({"event": "fill", "reason": reason, **fill.__dict__})
        return fill

    def _log(self, row: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
