"""Fast pricing loop for relationship arbitrage: every few seconds, re-quote
ONLY the markets in data/relation_watchlist.json (the Jev-confirmed
relations the last run_relation_scanner.py pass found), re-price every
relation, and paper-trade a violation the moment it appears.

Why a second loop: discovery (fetching ~16,000 markets, asking Jev) takes
about a minute and can't run every few seconds without getting rate-
limited by both venues. Confirmed relations don't change between scans --
only prices do -- and violations tend to last seconds, not the 30 minutes
between scans. Re-quoting ~100 watched markets is one Kalshi request and
one Polymarket request per ~100 markets, well within both venues' limits.

Each tick:
  1. Reload the watchlist if the scanner rewrote it.
  2. Batch-fetch top of book for every watched market (relation_sources
     fetch_*_quotes). A venue whose fetch fails has ALL its quotes cleared
     for the tick -- a stale price must never trade.
  3. Price every relation (relation_trading.price_relations).
  4. Write data/relation_live.json -- what the dashboard's /api/live
     endpoint serves, polled every few seconds.
  5. Log each violation to data/relation_arbs.jsonl when it first appears
     (not on every tick it persists), and with --paper buy every leg of a
     tradeable one, under the same ledger lock the scanner uses.

Never places a real order anywhere.

Usage:
    uv run python scripts/run_relation_watcher.py                    # price + log only
    uv run python scripts/run_relation_watcher.py --paper
    uv run python scripts/run_relation_watcher.py --interval-s 3 --ticks 5
"""
from __future__ import annotations

import argparse
import contextlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from kalshi_engine import ledger, relations  # noqa: E402
from kalshi_engine.kalshi_public import BASE as KALSHI_BASE  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.relation_sources import Quote, fetch_kalshi_quotes, fetch_polymarket_quotes  # noqa: E402
from kalshi_engine.relation_trading import (  # noqa: E402
    LIVE_PATH, WATCHLIST_PATH, append_arb_rows, arb_row, execute, load_watchlist, pair_id,
    price_relations, settled_payouts, write_json_atomic,
)

BACKOFF_MAX_S = 60.0


def apply_quotes(contracts: list[relations.Contract], quotes: dict[str, Quote]) -> None:
    """Update each contract in place; one missing from `quotes` (closed,
    or its venue's fetch failed) gets no prices, so it can't be traded."""
    for c in contracts:
        q = quotes.get(c.ticker)
        c.yes_bid, c.yes_ask = (q.yes_bid, q.yes_ask) if q else (None, None)
        c.yes_bid_size, c.yes_ask_size = (q.yes_bid_size, q.yes_ask_size) if q else (None, None)


def fetch_quotes(contracts: list[relations.Contract], client: httpx.Client) -> tuple[dict[str, Quote], list[str]]:
    kalshi = [c.ticker for c in contracts if c.venue == "kalshi"]
    poly = [c.ticker.removeprefix("PM-") for c in contracts if c.venue == "polymarket"]
    quotes: dict[str, Quote] = {}
    errors: list[str] = []
    for venue, fetch in (("kalshi", lambda: fetch_kalshi_quotes(kalshi, client)),
                         ("polymarket", lambda: fetch_polymarket_quotes(poly))):
        if not (kalshi if venue == "kalshi" else poly):
            continue
        try:
            quotes.update(fetch())
        except Exception as exc:  # noqa: BLE001 -- one venue down shouldn't stop the other
            errors.append(f"{venue}: {exc}")
    return quotes, errors


def live_row(p: dict, priced: dict) -> dict:
    a, b = p["a"], p["b"]

    def quote(c):
        return {"ticker": c.ticker, "bid": c.yes_bid, "ask": c.yes_ask}

    return {"id": pair_id(a, b), "status": priced["status"], "edge": priced["edge"], "note": priced["note"],
            "a": quote(a), "b": quote(b)}


def tick(pairs: list[dict], client: httpx.Client, open_signals: set[str], paper: bool) -> tuple[dict, list[str]]:
    contracts = list({id(c): c for p in pairs for c in (p["a"], p["b"])}.values())
    t0 = time.monotonic()
    quotes, errors = fetch_quotes(contracts, client)
    apply_quotes(contracts, quotes)
    fetch_ms = round((time.monotonic() - t0) * 1000)

    rows, violations = [], []
    for p in pairs:
        priced = price_relations(p["a"], p["b"], p["relations"])
        rows.append(live_row(p, priced))
        violations += [(arb, p["verdict"]) for arb in priced["arbs"] if arb.edge_per_set > 0]

    # A violation is logged (and traded) once when it appears, keyed on its
    # legs and prices -- a persisting one would otherwise log every tick.
    signals = {f"{arb.kind}:" + "|".join(f"{l.contract.ticker}/{l.side}@{l.price}" for l in arb.legs): (arb, v)
               for arb, v in violations}
    new = [signals[s] for s in signals if s not in open_signals]
    open_signals.clear()
    open_signals.update(signals)

    logged = []
    if new:
        new.sort(key=lambda o: o[0].edge_per_set, reverse=True)
        tradeable = paper and any(arb.tradeable for arb, _ in new)
        with ledger.ledger_lock() if tradeable else contextlib.nullcontext():
            broker = PaperBroker.from_ledger(settled=settled_payouts()) if tradeable else None
            held = set(broker.positions) if broker else set()
            for arb, verdict in new:
                row = arb_row(arb, verdict, source="watch")
                legs = " + ".join(f"{l.side.upper()} {l.contract.ticker} @{l.price:.2f}" for l in arb.legs)
                status = "tradeable" if arb.tradeable else f"skip ({arb.reason})"
                if broker is not None and arb.tradeable:
                    ok, why = execute(arb, broker, held, verdict)
                    row["traded"], row["trade_note"] = ok, why
                    status = "PAPER-TRADED" if ok else f"not traded ({why})"
                logged.append(f"[{arb.kind}] edge ${arb.edge_per_set:.3f}/set x{arb.qty}: {legs} -- {status}")
                append_arb_rows([row])

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    live = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs": len(pairs), "markets": len(contracts), "quoted": len(quotes),
        "fetch_ms": fetch_ms, "errors": errors, "counts": counts, "rows": rows,
    }
    write_json_atomic(LIVE_PATH, live)
    return live, logged


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-s", type=float, default=3.0, help="seconds between price checks")
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks, 0 = run forever")
    ap.add_argument("--paper", action="store_true", help="paper-trade every tradeable violation (all legs or none)")
    args = ap.parse_args()

    client = httpx.Client(base_url=KALSHI_BASE, timeout=10.0)
    pairs: list[dict] = []
    watch_mtime = None
    open_signals: set[str] = set()
    backoff = args.interval_s
    n = 0
    print(f"watching {WATCHLIST_PATH} every {args.interval_s:g}s{' (paper trading)' if args.paper else ''}. Ctrl+C to stop.",
          flush=True)
    try:
        while True:
            n += 1
            started = time.monotonic()
            try:
                mtime = WATCHLIST_PATH.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if mtime != watch_mtime:
                pairs, watch_mtime = load_watchlist(), mtime
                open_signals.clear()
                print(f"[{_now()}] watchlist: {len(pairs)} confirmed relation(s)", flush=True)
            if pairs:
                try:
                    live, logged = tick(pairs, client, open_signals, args.paper)
                except Exception as exc:  # noqa: BLE001 -- e.g. ledger lock timeout: retry next tick
                    print(f"[{_now()}] tick failed: {exc}", flush=True)
                    live, logged = {"errors": [str(exc)]}, []
                    open_signals.clear()  # re-evaluate anything this tick didn't get to
                for line in logged:
                    print(f"[{_now()}] {line}", flush=True)
                if live["errors"]:
                    print(f"[{_now()}] quote errors: {'; '.join(live['errors'])}", flush=True)
                    # Back off on errors (429s included) so a struggling venue isn't hammered.
                    backoff = min(backoff * 2, BACKOFF_MAX_S)
                else:
                    backoff = args.interval_s
            if args.ticks and n >= args.ticks:
                break
            time.sleep(max(0.0, backoff - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nstopped by user")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


if __name__ == "__main__":
    main()
