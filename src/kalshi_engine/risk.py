"""Hard risk limits. Never delegated to a model, never raised by a strategy.
Checked before every simulated order in paper_broker.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskLimits:
    # Paper sizes (owner's call): raised 2026-09-24, then again 2026-09-26 so
    # the wider universe (6x Polymarket, family rules, groups, walking the
    # book) isn't throttled by a $1k book tied up for weeks until settlement.
    max_order_notional_usd: float = 500.0
    max_position_usd: float = 500.0  # per ticker
    max_total_exposure_usd: float = 10000.0  # sum across all open positions (= starting paper cash)
    max_daily_loss_usd: float = 1000.0
    # If this file exists, every order is refused. touch it to halt trading,
    # delete it to resume. Checked fresh on every order, not cached.
    kill_switch_path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent / "data" / "KILL_SWITCH"
    )


class RiskVeto(Exception):
    """Raised with a plain-English reason when an order fails a hard limit."""


@dataclass
class RiskState:
    """Caller-owned bookkeeping, passed into check_order every time. Not
    persisted here -- paper_broker.py owns the source of truth."""
    position_usd_by_ticker: dict[str, float]
    total_exposure_usd: float
    realized_pnl_today_usd: float


def check_order(limits: RiskLimits, state: RiskState, ticker: str, notional_usd: float) -> None:
    """Raises RiskVeto if this order would violate a hard limit. Returns
    None (silently) if the order is allowed."""
    if limits.kill_switch_path.exists():
        raise RiskVeto(f"kill switch active ({limits.kill_switch_path})")

    if notional_usd > limits.max_order_notional_usd:
        raise RiskVeto(
            f"order notional ${notional_usd:.2f} exceeds max_order_notional_usd "
            f"${limits.max_order_notional_usd:.2f}"
        )

    current_position = state.position_usd_by_ticker.get(ticker, 0.0)
    if current_position + notional_usd > limits.max_position_usd:
        raise RiskVeto(
            f"position in {ticker} would reach ${current_position + notional_usd:.2f}, "
            f"exceeds max_position_usd ${limits.max_position_usd:.2f}"
        )

    if state.total_exposure_usd + notional_usd > limits.max_total_exposure_usd:
        raise RiskVeto(
            f"total exposure would reach ${state.total_exposure_usd + notional_usd:.2f}, "
            f"exceeds max_total_exposure_usd ${limits.max_total_exposure_usd:.2f}"
        )

    if state.realized_pnl_today_usd <= -limits.max_daily_loss_usd:
        raise RiskVeto(
            f"daily loss ${-state.realized_pnl_today_usd:.2f} has reached "
            f"max_daily_loss_usd ${limits.max_daily_loss_usd:.2f}"
        )
