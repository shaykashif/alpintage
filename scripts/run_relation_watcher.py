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

Two ways to get prices:

  - polling (default): every --interval-s, batch-fetch top of book --
    Kalshi's /markets, Polymarket's order book (CLOB /books).
  - --stream: hold both venues' market WebSockets open and re-price the
    moment a watched book changes (live_books.py). Kalshi's feed needs a
    signed handshake (KALSHI_KEY_ID + KALSHI_KEY_PATH in .env); without a
    key, Kalshi falls back to polling every --interval-s while Polymarket
    still streams. A feed that drops has its books cleared until it has
    reconnected and re-snapshotted -- a frozen book must never trade.

Never places a real order anywhere.

Usage:
    uv run python scripts/run_relation_watcher.py                    # price + log only
    uv run python scripts/run_relation_watcher.py --paper
    uv run python scripts/run_relation_watcher.py --interval-s 3 --ticks 5
    uv run python scripts/run_relation_watcher.py --stream --paper
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
import websockets  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from kalshi_engine import ledger, relations  # noqa: E402
from kalshi_engine.kalshi_public import BASE as KALSHI_BASE  # noqa: E402
from kalshi_engine.live_books import LiveBooks, SeqGap, kalshi_ws_headers  # noqa: E402
from kalshi_engine.paper_broker import PaperBroker  # noqa: E402
from kalshi_engine.relation_sources import (  # noqa: E402
    Quote, fetch_kalshi_quotes, fetch_polymarket_quotes, polymarket_yes_tokens,
)
from kalshi_engine.relation_trading import (  # noqa: E402
    LIVE_PATH, WATCHLIST_PATH, append_arb_rows, arb_row, execute, load_watchlist, pair_id,
    price_relations, settled_payouts, write_json_atomic,
)

BACKOFF_MAX_S = 60.0

KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"  # what the handshake signs
POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLY_PING_S = 10.0  # Polymarket expects a "PING" text frame every 10 s
MIN_PRICE_GAP_S = 0.1  # at most ~10 re-pricings a second however fast books move
LIVE_WRITE_S = 1.0  # relation_live.json at most once a second (the dashboard polls every few)


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


def unique_contracts(pairs: list[dict]) -> list[relations.Contract]:
    return list({id(c): c for p in pairs for c in (p["a"], p["b"])}.values())


def tick(pairs: list[dict], client: httpx.Client, open_signals: set[str], paper: bool) -> tuple[dict, list[str]]:
    """One polling pass: fetch every watched quote, then price and trade."""
    contracts = unique_contracts(pairs)
    t0 = time.monotonic()
    quotes, errors = fetch_quotes(contracts, client)
    fetch_ms = round((time.monotonic() - t0) * 1000)
    return price_and_trade(pairs, contracts, quotes, errors, open_signals, paper, {"fetch_ms": fetch_ms})


def price_and_trade(pairs: list[dict], contracts: list[relations.Contract], quotes: dict[str, Quote],
                    errors: list[str], open_signals: set[str], paper: bool, extra: dict | None = None,
                    write: bool = True) -> tuple[dict, list[str]]:
    """Apply `quotes`, re-price every relation, log (and with `paper`, buy)
    each violation once when it appears, and write relation_live.json."""
    apply_quotes(contracts, quotes)
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
        "pairs": len(pairs), "markets": len(contracts), "quoted": sum(1 for c in contracts if c.ticker in quotes),
        "errors": errors, "counts": counts, "rows": rows, **(extra or {}),
    }
    if write:
        write_json_atomic(LIVE_PATH, live)
    return live, logged


def write_waiting(watch_found: bool) -> None:
    """Heartbeat with nothing to watch, so "nothing to watch yet" is
    distinguishable from "watcher not running"."""
    write_json_atomic(LIVE_PATH, {
        "generated_at": datetime.now(timezone.utc).isoformat(), "pairs": 0, "markets": 0, "quoted": 0,
        "fetch_ms": 0, "errors": [], "counts": {}, "rows": [],
        "waiting": "watchlist has no confirmed relations" if watch_found else "no watchlist yet",
    })


# ---- Streaming -----------------------------------------------------------------

class StreamState:
    """Shared by the feed tasks and the pricer (all on one event loop)."""

    def __init__(self) -> None:
        self.books = LiveBooks()
        self.polled: dict[str, Quote] = {}  # Kalshi quotes when there's no key to stream with
        self.pairs: list[dict] = []
        self.generation = 0  # bumped when the watchlist changes: feeds resubscribe
        self.changed = asyncio.Event()  # a watched book moved
        self.feed: dict[str, str] = {"kalshi": "starting", "polymarket": "starting"}

    def tickers(self, venue: str) -> list[str]:
        return sorted({c.ticker for c in unique_contracts(self.pairs) if c.venue == venue})


async def _until_generation_changes(state: StreamState, gen: int) -> None:
    while state.generation == gen:
        await asyncio.sleep(0.5)


async def _feed_forever(state: StreamState, venue: str, connect_once) -> None:
    """Run one venue's socket, reconnecting with backoff. Whenever it isn't
    connected, that venue's books are cleared so nothing stale trades."""
    backoff = 1.0
    while True:
        gen = state.generation
        if not state.tickers(venue):
            state.feed[venue] = "idle"
            await _until_generation_changes(state, gen)
            continue
        try:
            if await connect_once(gen):
                backoff = 1.0
            state.feed[venue] = "resubscribing"
        except (SeqGap, OSError, websockets.WebSocketException, RuntimeError, ValueError,
                httpx.HTTPError, asyncio.TimeoutError) as exc:
            state.feed[venue] = f"down ({type(exc).__name__}: {exc})"[:160]
            print(f"[{_now()}] {venue} feed {state.feed[venue]} -- retrying in {backoff:.0f}s", flush=True)
            state.books.clear_venue(venue)
            state.changed.set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            continue
        state.books.clear_venue(venue)
        state.changed.set()


async def kalshi_feed(state: StreamState, key_id: str, key_path: str) -> None:
    async def connect_once(gen: int) -> bool:
        tickers = state.tickers("kalshi")
        headers = kalshi_ws_headers(key_id, key_path, KALSHI_WS_PATH)
        got = False
        async with websockets.connect(KALSHI_WS_URL, additional_headers=headers, ping_interval=10, open_timeout=15) as ws:
            await ws.send(json.dumps({"id": 1, "cmd": "subscribe",
                                      "params": {"channels": ["orderbook_delta"], "market_tickers": tickers}}))
            state.feed["kalshi"] = f"streaming {len(tickers)}"
            while state.generation == gen:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if state.books.apply_kalshi(json.loads(raw)):
                    got = True
                    state.changed.set()
        return got

    await _feed_forever(state, "kalshi", connect_once)


async def kalshi_poll(state: StreamState, interval_s: float) -> None:
    """No API key: poll Kalshi's /markets instead."""
    client = httpx.Client(base_url=KALSHI_BASE, timeout=10.0)
    backoff = interval_s
    while True:
        tickers = state.tickers("kalshi")
        if tickers:
            try:
                state.polled = await asyncio.to_thread(fetch_kalshi_quotes, tickers, client)
                state.feed["kalshi"] = f"polling {len(tickers)} every {interval_s:g}s (no API key)"
                backoff = interval_s
            except Exception as exc:  # noqa: BLE001 -- clear, back off, retry
                state.polled = {}
                state.feed["kalshi"] = f"down ({exc})"[:160]
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            state.changed.set()
        else:
            state.feed["kalshi"] = "idle"
        await asyncio.sleep(backoff)


async def polymarket_feed(state: StreamState) -> None:
    async def connect_once(gen: int) -> bool:
        slugs = [t.removeprefix("PM-") for t in state.tickers("polymarket")]
        tokens = await asyncio.to_thread(polymarket_yes_tokens, slugs)
        state.books.poly_ticker_of = {tok: f"PM-{slug}" for slug, tok in tokens.items()}
        if not tokens:  # every watched market closed: nothing to subscribe to
            state.feed["polymarket"] = "idle (no open markets)"
            await _until_generation_changes(state, gen)
            return False
        got = False
        async with websockets.connect(POLY_WS_URL, ping_interval=None, open_timeout=15) as ws:
            await ws.send(json.dumps({"assets_ids": list(tokens.values()), "type": "market"}))
            state.feed["polymarket"] = f"streaming {len(tokens)}"
            last_ping = time.monotonic()
            while state.generation == gen:
                if time.monotonic() - last_ping >= POLY_PING_S:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if raw == "PONG":
                    continue
                body = json.loads(raw)
                for event in body if isinstance(body, list) else [body]:
                    if state.books.apply_polymarket(event):
                        got = True
                        state.changed.set()
        return got

    await _feed_forever(state, "polymarket", connect_once)


async def pricer(state: StreamState, paper: bool) -> None:
    """Re-price whenever a book moves (at most every MIN_PRICE_GAP_S), and at
    least once a second so the watchlist and the live file stay fresh."""
    open_signals: set[str] = set()
    watch_mtime: float | None = -1.0
    last_write = 0.0
    while True:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.changed.wait(), timeout=1.0)
        state.changed.clear()

        try:
            mtime = WATCHLIST_PATH.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime != watch_mtime:
            state.pairs, watch_mtime = load_watchlist(), mtime
            state.generation += 1
            open_signals.clear()
            print(f"[{_now()}] watchlist: {len(state.pairs)} confirmed relation(s)", flush=True)

        now = time.monotonic()
        if not state.pairs:
            if now - last_write >= LIVE_WRITE_S:
                write_waiting(watch_mtime is not None)
                last_write = now
            continue

        quotes = {**state.polled, **state.books.quotes()}
        errors = [f"{v}: {s}" for v, s in state.feed.items() if s.startswith("down")]
        write = now - last_write >= LIVE_WRITE_S
        try:
            _, logged = await asyncio.to_thread(
                price_and_trade, state.pairs, unique_contracts(state.pairs), quotes, errors, open_signals, paper,
                {"mode": "stream", "feeds": dict(state.feed)}, write)
        except Exception as exc:  # noqa: BLE001 -- e.g. ledger lock timeout: re-evaluate next pass
            print(f"[{_now()}] pricing failed: {exc}", flush=True)
            open_signals.clear()
            logged = []
        if write:
            last_write = now
        for line in logged:
            print(f"[{_now()}] {line}", flush=True)
        await asyncio.sleep(max(0.0, MIN_PRICE_GAP_S - (time.monotonic() - now)))


async def run_stream(paper: bool, interval_s: float) -> None:
    state = StreamState()
    key_id, key_path = os.environ.get("KALSHI_KEY_ID"), os.environ.get("KALSHI_KEY_PATH")
    if key_id and key_path and os.path.exists(key_path):
        kalshi = kalshi_feed(state, key_id, key_path)
        print("kalshi: streaming order books", flush=True)
    else:
        kalshi = kalshi_poll(state, interval_s)
        print("kalshi: no KALSHI_KEY_ID/KALSHI_KEY_PATH (or key file missing) -- polling instead", flush=True)
    await asyncio.gather(kalshi, polymarket_feed(state), pricer(state, paper))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-s", type=float, default=3.0, help="seconds between price checks")
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks, 0 = run forever")
    ap.add_argument("--paper", action="store_true", help="paper-trade every tradeable violation (all legs or none)")
    ap.add_argument("--stream", action="store_true",
                    help="stream order books over WebSockets; --interval-s then only paces Kalshi polling when there's no key")
    args = ap.parse_args()
    load_dotenv()

    if args.stream:
        print(f"streaming books for {WATCHLIST_PATH}{' (paper trading)' if args.paper else ''}. Ctrl+C to stop.", flush=True)
        try:
            asyncio.run(run_stream(args.paper, args.interval_s))
        except KeyboardInterrupt:
            print("\nstopped by user")
        return

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
            else:
                write_waiting(watch_mtime is not None)
            if args.ticks and n >= args.ticks:
                break
            time.sleep(max(0.0, backoff - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nstopped by user")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


if __name__ == "__main__":
    main()
