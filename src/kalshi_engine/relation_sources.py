"""Fetches short-dated markets from Kalshi and Polymarket, keeps only the
CULTURAL, ECONOMIC and GEOPOLITICAL events (the project owner's scope; decided by
topics.py -- rules for clear-cut categories, Jev for grey areas), and turns
them into venue-neutral relations.Contract objects with every bit of
resolution detail each venue exposes packed into `context` for Jev.

Two phases, so topic judgment happens once per EVENT before any contract
is built:  fetch_*_events() -> topic_subjects() -> TopicClassifier ->
*_contracts(events, topics).

Verified live 2026-09-24:
- Kalshi GET /events with with_nested_markets + min_close_ts/max_close_ts
  paginates to completion (~70 pages, ~25s). The window filters EVENTS, so
  some nested markets still close later -- re-filtered per market here.
  Events carry category, mutually_exclusive and settlement_sources; markets
  carry top-of-book sizes (yes_bid_size_fp / yes_ask_size_fp).
- Polymarket gamma /markets honours end_date_min/end_date_max. Markets
  carry bestBid/bestAsk (for outcome[0]) and a per-market feeSchedule. It
  returns no category (600/600 None), so its fee-schedule name
  (culture_fees, sports_fees_v3, ...) is the topic hint instead.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from .kalshi_public import BASE as KALSHI_BASE
from .polymarket_client import BASE as POLY_BASE
from .relations import Contract
from .topics import KALSHI_RULES, Subject

PAGE_PACING_S = 0.15


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _resolves_by(m: dict, horizon: datetime) -> bool:
    """Closes AND is expected to settle inside the horizon -- a market that
    stops trading next week but settles in a month ties capital up for a
    month, which isn't 'upcoming'."""
    close = _parse_ts(m.get("close_time"))
    settle = _parse_ts(m.get("expected_expiration_time")) or close
    return close is not None and settle is not None and settle <= horizon


@dataclass
class VenueEvent:
    """One event and its in-horizon markets, before topic filtering."""
    venue: str
    event_id: str
    title: str
    hint: str | None  # Kalshi category / Polymarket fee-schedule name
    raw: dict
    markets: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.venue}:{self.event_id}"


def topic_subjects(events: list[VenueEvent]) -> list[Subject]:
    subjects = []
    for e in events:
        if e.venue == "kalshi":
            sample = [m.get("yes_sub_title") or m.get("title", "") for m in e.markets]
        else:
            sample = [m.get("question", "") for m in e.markets]
        subjects.append(Subject(key=e.key, venue=e.venue, title=e.title, hint=e.hint, sample=[s for s in sample if s]))
    return subjects


# ---- Kalshi ------------------------------------------------------------------

_URL = re.compile(r"https?://([^/\s\"')]+)", re.I)


def _host(match: re.Match | None) -> str | None:
    if not match:
        return None
    host = match.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def kalshi_settlement(event: dict, m: dict) -> str | None:
    """"venue|sources|when" -- two markets with the same key settle off the
    same source at the same moment (relations.MAX_PLAUSIBLE_EDGE_SAME_SETTLEMENT)."""
    names = sorted({s["name"].strip().lower() for s in event.get("settlement_sources") or [] if s.get("name")})
    when = m.get("expected_expiration_time") or m.get("close_time")
    return f"kalshi|{','.join(names)}|{when}" if names and when else None


def polymarket_settlement(pm: dict) -> str | None:
    """Same key for Polymarket. resolutionSource is usually empty, so the
    source is the site the rules link to (e.g. binance.com)."""
    host = _host(_URL.search(pm.get("resolutionSource") or "")) or _host(_URL.search(pm.get("description") or ""))
    end = pm.get("endDate")
    return f"polymarket|{host}|{end}" if host and end else None


def kalshi_context(event: dict, m: dict) -> str:
    sources = ", ".join(s.get("name", "") for s in event.get("settlement_sources") or [] if s.get("name"))
    lines = [
        "Venue: Kalshi",
        f"Event: {event.get('title', '')}" + (f" -- {event.get('sub_title')}" if event.get("sub_title") else ""),
        f"Market: {m.get('title', '')}",
        f"YES means: {m.get('yes_sub_title') or m.get('title', '')}",
        f"Rules: {m.get('rules_primary', '')}",
    ]
    if m.get("rules_secondary"):
        lines.append(f"Additional rules: {m['rules_secondary']}")
    if sources:
        lines.append(f"Settlement sources: {sources}")
    lines.append(f"Trading closes: {m.get('close_time')}; expected settlement: {m.get('expected_expiration_time')}")
    if m.get("can_close_early"):
        lines.append(f"Can close early: {m.get('early_close_condition') or 'yes'}")
    if event.get("mutually_exclusive"):
        lines.append("Kalshi flags the markets in this event as mutually exclusive.")
    return "\n".join(lines)


def fetch_kalshi_events(horizon_days: float = 21.0, max_pages: int = 200) -> list[VenueEvent]:
    """Every event with at least one active market settling in the horizon.
    Categories that rules exclude outright (Sports, Elections, ...) are
    dropped here already -- no point carrying thousands of them further."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    client = httpx.Client(base_url=KALSHI_BASE, timeout=30.0)
    out: list[VenueEvent] = []
    cursor = None
    for _ in range(max_pages):
        params = {
            "status": "open", "with_nested_markets": "true", "limit": 200,
            "min_close_ts": int(now.timestamp()), "max_close_ts": int(horizon.timestamp()),
        }
        if cursor:
            params["cursor"] = cursor
        r = client.get("/events", params=params)
        r.raise_for_status()
        body = r.json()
        for event in body.get("events", []):
            cat = event.get("category")
            if cat in KALSHI_RULES and KALSHI_RULES[cat] is None:
                continue
            markets = [m for m in event.get("markets") or [] if m.get("status") == "active" and _resolves_by(m, horizon)]
            if markets:
                out.append(VenueEvent("kalshi", event["event_ticker"], event.get("title", ""), cat, event, markets))
        cursor = body.get("cursor")
        if not cursor:
            break
        time.sleep(PAGE_PACING_S)
    return out


def kalshi_contracts(events: list[VenueEvent], topics: dict[str, str | None]) -> list[Contract]:
    out = []
    for e in events:
        topic = topics.get(e.key)
        if e.venue != "kalshi" or not topic:
            continue
        for m in e.markets:
            out.append(Contract(
                venue="kalshi", ticker=m["ticker"], event_id=e.event_id, category=e.hint,
                title=f"{m.get('title', '')} -- {m.get('yes_sub_title') or ''}",
                context=kalshi_context(e.raw, m), close_time=m.get("close_time"),
                yes_bid=_f(m.get("yes_bid_dollars")), yes_ask=_f(m.get("yes_ask_dollars")),
                yes_bid_size=_f(m.get("yes_bid_size_fp")), yes_ask_size=_f(m.get("yes_ask_size_fp")),
                event_mutually_exclusive=bool(e.raw.get("mutually_exclusive")), topic=topic,
                settlement=kalshi_settlement(e.raw, m),
            ))
    return out


# ---- Polymarket ----------------------------------------------------------------

def _is_sports(pm: dict) -> bool:
    return bool(pm.get("sportsMarketType") or pm.get("gameStartTime")
                or str(pm.get("feeType") or "").startswith("sports"))


def polymarket_context(pm: dict) -> str:
    event = (pm.get("events") or [{}])[0]
    outcomes = json.loads(pm.get("outcomes") or "[]")
    return "\n".join([
        "Venue: Polymarket",
        f"Event: {event.get('title', '')}",
        f"Question: {pm.get('question', '')}",
        f"YES means: outcome '{outcomes[0] if outcomes else 'Yes'}'",
        f"Rules: {pm.get('description', '')}",
        f"Resolution source: {pm.get('resolutionSource') or 'not stated'}",
        f"Trading ends: {pm.get('endDate')}",
    ])


SPORTS_TAG_ID = 1  # Gamma's "Sports" tag (GET /tags/slug/sports)


def fetch_polymarket_events(horizon_days: float = 21.0, page_size: int = 100) -> list[VenueEvent]:
    """Plain Yes/No, non-sports markets ending in the horizon, grouped by
    their Polymarket event.

    Walks GET /events/keyset (cursor pagination, no offset ceiling) with
    sports excluded server-side. The old /markets walk was sorted by volume
    and capped at ~2,000 rows per window; sports were 85-98% of those rows,
    so most non-sports markets were never seen (measured 2026-09-26: ~850
    in scope; this walk finds ~9,000 open non-sports Yes/No markets in ~40 s)."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days)
    params = {"closed": "false", "active": "true", "limit": page_size, "exclude_tag_id": SPORTS_TAG_ID,
              "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end_date_max": horizon.strftime("%Y-%m-%dT%H:%M:%SZ")}
    by_event: dict[str, VenueEvent] = {}
    seen: set[str] = set()
    cursor = None
    while True:
        r = httpx.get(f"{POLY_BASE}/events/keyset", params={**params, **({"after_cursor": cursor} if cursor else {})},
                      timeout=30.0)
        r.raise_for_status()
        body = r.json()
        for event in body.get("events") or []:
            header = {k: v for k, v in event.items() if k != "markets"}
            for pm in event.get("markets") or []:
                if pm.get("slug") in seen or pm.get("closed") or _is_sports(pm) or not pm.get("acceptingOrders", True):
                    continue
                end = pm.get("endDate")
                if end and end > horizon.strftime("%Y-%m-%dT%H:%M:%SZ"):
                    continue
                try:
                    outcomes = json.loads(pm.get("outcomes") or "[]")
                except json.JSONDecodeError:
                    continue
                if [o.lower() for o in outcomes] != ["yes", "no"]:
                    continue  # keep plain Yes/No markets: YES has one unambiguous meaning
                seen.add(pm["slug"])
                pm.setdefault("events", [header])  # the shape GET /markets returns, which the rest of the code reads
                eid = event.get("slug") or pm["slug"]
                ve = by_event.get(eid)
                if ve is None:
                    ve = by_event[eid] = VenueEvent("polymarket", eid, event.get("title") or pm.get("question", ""),
                                                    pm.get("feeType"), header)
                ve.markets.append(pm)
        cursor = body.get("next_cursor")
        if not cursor or not body.get("events"):
            break
        time.sleep(PAGE_PACING_S)
    return list(by_event.values())


def polymarket_contracts(events: list[VenueEvent], topics: dict[str, str | None]) -> list[Contract]:
    out = []
    for e in events:
        topic = topics.get(e.key)
        if e.venue != "polymarket" or not topic:
            continue
        for pm in e.markets:
            sched = pm.get("feeSchedule") or {}
            fees_on = pm.get("feesEnabled") and pm.get("feeType") != "zero_fees"
            out.append(Contract(
                venue="polymarket", ticker=f"PM-{pm['slug']}", event_id=f"PM-{e.event_id}", category=pm.get("feeType"),
                title=pm.get("question", ""), context=polymarket_context(pm), close_time=pm.get("endDate"),
                yes_bid=_f(pm.get("bestBid")), yes_ask=_f(pm.get("bestAsk")),
                fee_rate=float(sched.get("rate", 0.0)) if fees_on else 0.0,
                fee_exponent=float(sched.get("exponent", 1.0)), topic=topic,
                settlement=polymarket_settlement(pm),
                opened_at=pm.get("createdAt") or pm.get("startDate"),
                # negRisk: winner-take-all event, at most one market resolves YES.
                event_mutually_exclusive=bool(e.raw.get("negRisk") or pm.get("negRisk")),
            ))
    return out


# ---- Fast quote refresh (the watcher's every-few-seconds loop) ------------------

@dataclass
class Quote:
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_size: float | None = None
    yes_ask_size: float | None = None
    # Depth, best first, [price, size]: YES bids descending, YES asks ascending.
    bid_levels: list | None = None
    ask_levels: list | None = None


BOOK_DEPTH = 10  # levels kept per side for walking the book (relations._walk)


QUOTE_BATCH = 100  # both venues accept ~100 identifiers per request (verified live 2026-09-24)


def fetch_kalshi_quotes(tickers: list[str], client: httpx.Client | None = None) -> dict[str, Quote]:
    """Top of book for specific Kalshi markets: GET /markets?tickers=a,b,...
    returned all 67 watched markets in one ~0.4 s call."""
    client = client or httpx.Client(base_url=KALSHI_BASE, timeout=10.0)
    out: dict[str, Quote] = {}
    for i in range(0, len(tickers), QUOTE_BATCH):
        r = client.get("/markets", params={"tickers": ",".join(tickers[i:i + QUOTE_BATCH]), "limit": 1000})
        r.raise_for_status()
        for m in r.json().get("markets", []):
            if m.get("status") != "active":
                continue  # closed/settled: no longer tradeable, leave it unquoted
            out[m["ticker"]] = Quote(_f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars")),
                                     _f(m.get("yes_bid_size_fp")), _f(m.get("yes_ask_size_fp")))
    return out


CLOB_BASE = "https://clob.polymarket.com"

# slug -> YES outcome token id. A market's token ids never change, so each
# is looked up once per process (Gamma) and reused by every tick and stream.
_YES_TOKENS: dict[str, str] = {}


def polymarket_yes_tokens(slugs: list[str]) -> dict[str, str]:
    """{slug: YES token id} for open markets. The order book and the market
    WebSocket are keyed by token, not slug. clobTokenIds pairs with
    `outcomes`; the token whose outcome is "Yes" is the YES book."""
    missing = [s for s in dict.fromkeys(slugs) if s not in _YES_TOKENS]
    for i in range(0, len(missing), QUOTE_BATCH):
        batch = missing[i:i + QUOTE_BATCH]
        r = httpx.get(f"{POLY_BASE}/markets", params=[("slug", s) for s in batch] + [("limit", QUOTE_BATCH)], timeout=10.0)
        r.raise_for_status()
        for m in r.json():
            if m.get("closed") or not m.get("acceptingOrders", True):
                continue
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            outcomes = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
            if "yes" in outcomes and len(tokens) == len(outcomes):
                _YES_TOKENS[m["slug"]] = tokens[outcomes.index("yes")]
    return {s: _YES_TOKENS[s] for s in slugs if s in _YES_TOKENS}


def top_of_book(bids: list[dict], asks: list[dict]) -> Quote:
    """Best bid/ask (and the size resting there) from CLOB levels. Level
    order isn't relied on: the REST book lists bids ascending and asks
    descending, the stream makes no promise."""
    bid_lv = sorted(([float(lv["price"]), float(lv["size"])] for lv in bids), reverse=True)[:BOOK_DEPTH]
    ask_lv = sorted([float(lv["price"]), float(lv["size"])] for lv in asks)[:BOOK_DEPTH]
    return Quote(bid_lv[0][0] if bid_lv else None, ask_lv[0][0] if ask_lv else None,
                 bid_lv[0][1] if bid_lv else None, ask_lv[0][1] if ask_lv else None, bid_lv or None, ask_lv or None)


def fetch_polymarket_quotes(slugs: list[str]) -> dict[str, Quote]:
    """Best bid/ask for specific Polymarket markets, keyed by "PM-<slug>",
    read from the live order book (CLOB POST /books) -- not Gamma's
    bestBid/bestAsk, which trailed the book (seen live 2026-09-24: Gamma
    0.6c/4.4c while the book stood at 0.3c/3.3c)."""
    tokens = polymarket_yes_tokens(slugs)
    slug_of = {t: s for s, t in tokens.items()}
    ids = list(slug_of)
    out: dict[str, Quote] = {}
    for i in range(0, len(ids), QUOTE_BATCH):
        r = httpx.post(f"{CLOB_BASE}/books", json=[{"token_id": t} for t in ids[i:i + QUOTE_BATCH]], timeout=10.0)
        r.raise_for_status()
        for book in r.json():
            slug = slug_of.get(book.get("asset_id"))
            if slug:
                out[f"PM-{slug}"] = top_of_book(book.get("bids") or [], book.get("asks") or [])
    return out


def fetch_polymarket_market(slug: str) -> dict | None:
    """One Polymarket market by slug, for scoring a paper position. Gamma
    leaves closed markets out of a slug lookup unless asked for them
    (seen live 2026-09-26: two resolved SOL markets came back empty, so
    their positions sat "unknown" instead of settling) -- so a miss is
    retried with closed=true."""
    for params in ({"slug": slug}, {"slug": slug, "closed": "true"}):
        r = httpx.get(f"{POLY_BASE}/markets", params=params, timeout=20.0)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0]
    return None


def polymarket_as_kalshi_shape(pm: dict) -> dict:
    """Map a Polymarket market onto the few Kalshi fields the paper scorer
    reads (status/result/yes_bid_dollars/yes_ask_dollars), so settlement
    and mark-to-market work the same way for both venues."""
    prices = [float(p) for p in json.loads(pm.get("outcomePrices") or "[]")]
    shaped = {"yes_bid_dollars": pm.get("bestBid"), "yes_ask_dollars": pm.get("bestAsk"), "status": "active"}
    # Polymarket's own taker fee, so the scorer charges each venue's exit fee.
    sched = pm.get("feeSchedule") or {}
    fees_on = pm.get("feesEnabled") and pm.get("feeType") != "zero_fees"
    shaped["fee_rate"] = float(sched.get("rate", 0.0)) if fees_on else 0.0
    shaped["fee_exponent"] = float(sched.get("exponent", 1.0))
    if pm.get("closed") and prices:
        if prices[0] >= 0.99:
            shaped.update(status="settled", result="yes")
        elif prices[0] <= 0.01:
            shaped.update(status="settled", result="no")
    return shaped
