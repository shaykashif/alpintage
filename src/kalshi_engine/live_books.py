"""Streaming order books for the relation watcher: Kalshi's and Polymarket's
market WebSockets, folded into one top-of-book Quote per watched market.

Pure message handling lives here (LiveBooks, kalshi_ws_headers) so it can be
tested with recorded messages; the socket loops live in
scripts/run_relation_watcher.py.

Kalshi (docs.kalshi.com, orderbook_delta channel): an `orderbook_snapshot`
then `orderbook_delta`s per market. Both sides are BIDS -- yes_dollars_fp
are YES bids, no_dollars_fp are NO bids -- so the YES ask is 1 minus the
best NO bid, and the size resting there is that NO bid's size. `seq`
increments per subscription (sid); a gap means a missed delta, and the
books can no longer be trusted, so it raises SeqGap and the caller
reconnects from fresh snapshots.

Polymarket (docs.polymarket.com, market channel): `book` replaces a token's
whole book; `price_change` sets the new aggregate size at one price level
(side BUY = bid, SELL = ask; size 0 removes the level). Only each market's
YES token is subscribed, so its bids/asks are the YES bid/ask.

Never places an order.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

from .relation_sources import Quote


class SeqGap(Exception):
    """A Kalshi delta was skipped: rebuild the books from a new snapshot."""


def _f(v) -> float:
    return float(v)


class _Book:
    __slots__ = ("bids", "asks")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    @staticmethod
    def set_level(side: dict[float, float], price: float, size: float) -> None:
        if size > 0:
            side[price] = size
        else:
            side.pop(price, None)


class LiveBooks:
    """Latest top of book per ticker ("<kalshi ticker>" or "PM-<slug>").
    `quotes()` is what the pricer reads; a venue that disconnects is
    cleared (`clear_venue`) so a frozen book can never trade."""

    def __init__(self) -> None:
        self._kalshi: dict[str, _Book] = {}  # bids = YES bids, asks keyed by NO bid price
        self._poly: dict[str, _Book] = {}  # by PM ticker
        self._kalshi_seq: dict[int, int] = {}
        self.poly_ticker_of: dict[str, str] = {}  # YES token id -> "PM-<slug>"
        self.updated_at: dict[str, float] = {"kalshi": 0.0, "polymarket": 0.0}

    # ---- Kalshi --------------------------------------------------------------
    def apply_kalshi(self, message: dict) -> str | None:
        """Fold one Kalshi message in; returns the ticker it changed, if any."""
        kind = message.get("type")
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            if kind == "error":
                raise RuntimeError(f"kalshi ws error: {message.get('msg')}")
            return None
        sid, seq = message.get("sid"), message.get("seq")
        if seq is not None:
            last = self._kalshi_seq.get(sid)
            if last is not None and seq != last + 1:
                raise SeqGap(f"kalshi sid {sid}: seq {last} -> {seq}")
            self._kalshi_seq[sid] = seq
        m = message["msg"]
        ticker = m["market_ticker"]
        if kind == "orderbook_snapshot":
            book = self._kalshi[ticker] = _Book()
            for price, size in m.get("yes_dollars_fp") or []:
                _Book.set_level(book.bids, _f(price), _f(size))
            for price, size in m.get("no_dollars_fp") or []:
                _Book.set_level(book.asks, _f(price), _f(size))
        else:
            book = self._kalshi.get(ticker)
            if book is None:
                return None  # delta before its snapshot: wait for the snapshot
            side = book.bids if m.get("side") == "yes" else book.asks
            price = _f(m["price_dollars"])
            _Book.set_level(side, price, round(side.get(price, 0.0) + _f(m["delta_fp"]), 6))
        self.updated_at["kalshi"] = time.monotonic()
        return ticker

    # ---- Polymarket ----------------------------------------------------------
    def apply_polymarket(self, message: dict) -> list[str]:
        """Fold one Polymarket event in; returns the tickers it changed."""
        kind = message.get("event_type")
        changed: list[str] = []
        if kind == "book":
            ticker = self.poly_ticker_of.get(message.get("asset_id"))
            if ticker:
                book = self._poly[ticker] = _Book()
                for lv in message.get("bids") or []:
                    _Book.set_level(book.bids, _f(lv["price"]), _f(lv["size"]))
                for lv in message.get("asks") or []:
                    _Book.set_level(book.asks, _f(lv["price"]), _f(lv["size"]))
                changed.append(ticker)
        elif kind == "price_change":
            for ch in message.get("price_changes") or []:
                ticker = self.poly_ticker_of.get(ch.get("asset_id"))
                book = self._poly.get(ticker) if ticker else None
                if book is None:
                    continue  # no snapshot yet for this token
                side = book.bids if ch.get("side") == "BUY" else book.asks
                _Book.set_level(side, _f(ch["price"]), _f(ch["size"]))
                changed.append(ticker)
        if changed:
            self.updated_at["polymarket"] = time.monotonic()
        return changed

    # ---- Reading -------------------------------------------------------------
    def clear_venue(self, venue: str) -> None:
        if venue == "kalshi":
            self._kalshi.clear()
            self._kalshi_seq.clear()
        else:
            self._poly.clear()

    def quotes(self) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for ticker, b in self._kalshi.items():
            bid = max(b.bids, default=None)
            no_bid = max(b.asks, default=None)
            out[ticker] = Quote(
                bid, round(1 - no_bid, 4) if no_bid is not None else None,
                b.bids[bid] if bid is not None else None, b.asks[no_bid] if no_bid is not None else None,
            )
        for ticker, b in self._poly.items():
            bid = max(b.bids, default=None)
            ask = min(b.asks, default=None)
            out[ticker] = Quote(bid, ask, b.bids[bid] if bid is not None else None,
                                b.asks[ask] if ask is not None else None)
        return out


def kalshi_ws_headers(key_id: str, key_path: str | Path, path: str = "/trade-api/ws/v2") -> dict[str, str]:
    """Signed handshake headers (docs.kalshi.com, WebSocket quick start):
    sign timestamp_ms + "GET" + path -- RSA-PSS/SHA-256 with a digest-length
    salt for RSA keys, plain signing for Ed25519 -- base64-encoded."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    ts = str(int(time.time() * 1000))
    message = (ts + "GET" + path).encode()
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                       hashes.SHA256())
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(message)
    else:
        raise ValueError(f"unsupported Kalshi key type: {type(key).__name__}")
    return {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}
