import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import relation_sources  # noqa: E402
from kalshi_engine.live_books import LiveBooks, SeqGap, kalshi_ws_headers  # noqa: E402


def k_snapshot(seq=1, sid=2, ticker="FED-23DEC-T3.00"):
    # Shape from docs.kalshi.com (orderbook_snapshot): both sides are bids.
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": {
        "market_ticker": ticker,
        "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
        "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
    }}


def k_delta(seq, price, delta, side, sid=2, ticker="FED-23DEC-T3.00"):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq, "msg": {
        "market_ticker": ticker, "price_dollars": price, "delta_fp": delta, "side": side}}


def test_kalshi_snapshot_gives_yes_ask_from_best_no_bid():
    books = LiveBooks()
    assert books.apply_kalshi(k_snapshot()) == "FED-23DEC-T3.00"
    q = books.quotes()["FED-23DEC-T3.00"]
    assert (q.yes_bid, q.yes_bid_size) == (0.22, 333.0)
    assert (q.yes_ask, q.yes_ask_size) == (0.44, 146.0)  # 1 - best NO bid 0.56


def test_kalshi_deltas_add_and_remove_levels():
    books = LiveBooks()
    books.apply_kalshi(k_snapshot())
    books.apply_kalshi(k_delta(2, "0.2200", "-333.00", "yes"))  # best YES bid pulled
    books.apply_kalshi(k_delta(3, "0.5700", "10.00", "no"))  # better NO bid
    q = books.quotes()["FED-23DEC-T3.00"]
    assert (q.yes_bid, q.yes_bid_size) == (0.08, 300.0)
    assert (q.yes_ask, q.yes_ask_size) == (0.43, 10.0)


def test_kalshi_sequence_gap_raises_so_books_rebuild():
    books = LiveBooks()
    books.apply_kalshi(k_snapshot(seq=1))
    with pytest.raises(SeqGap):
        books.apply_kalshi(k_delta(3, "0.2200", "1.00", "yes"))


def test_kalshi_delta_before_snapshot_is_ignored():
    books = LiveBooks()
    assert books.apply_kalshi(k_delta(1, "0.2200", "5.00", "yes")) is None
    assert books.quotes() == {}


def test_kalshi_error_message_raises():
    with pytest.raises(RuntimeError):
        LiveBooks().apply_kalshi({"type": "error", "msg": {"code": 6, "msg": "Already subscribed"}})


def poly_books():
    books = LiveBooks()
    books.poly_ticker_of = {"111": "PM-some-market"}
    # Shape from docs.polymarket.com (market channel "book").
    books.apply_polymarket({"event_type": "book", "asset_id": "111",
                            "bids": [{"price": "0.08", "size": "300"}, {"price": "0.07", "size": "50"}],
                            "asks": [{"price": "0.99", "size": "10"}, {"price": "0.12", "size": "40"}]})
    return books


def test_polymarket_book_and_price_changes():
    books = poly_books()
    q = books.quotes()["PM-some-market"]
    assert (q.yes_bid, q.yes_ask, q.yes_bid_size, q.yes_ask_size) == (0.08, 0.12, 300.0, 40.0)
    changed = books.apply_polymarket({"event_type": "price_change", "price_changes": [
        {"asset_id": "111", "price": "0.12", "size": "0", "side": "SELL"},  # best ask taken out
        {"asset_id": "111", "price": "0.09", "size": "25", "side": "BUY"},  # new best bid
        {"asset_id": "999", "price": "0.5", "size": "1", "side": "BUY"},  # not watched
    ]})
    assert changed == ["PM-some-market", "PM-some-market"]
    q = books.quotes()["PM-some-market"]
    assert (q.yes_bid, q.yes_ask, q.yes_bid_size, q.yes_ask_size) == (0.09, 0.99, 25.0, 10.0)


def test_clearing_a_venue_drops_its_quotes_only():
    books = poly_books()
    books.apply_kalshi(k_snapshot())
    books.clear_venue("polymarket")
    assert set(books.quotes()) == {"FED-23DEC-T3.00"}
    books.clear_venue("kalshi")
    assert books.quotes() == {}
    books.apply_kalshi(k_snapshot(seq=7))  # a fresh subscription may start at any seq
    assert "FED-23DEC-T3.00" in books.quotes()


def test_rest_book_top_ignores_level_order():
    # CLOB /books lists bids ascending and asks descending.
    q = relation_sources.top_of_book(
        [{"price": "0.001", "size": "104"}, {"price": "0.003", "size": "13.06"}],
        [{"price": "0.999", "size": "4402"}, {"price": "0.033", "size": "40"}],
    )
    assert (q.yes_bid, q.yes_ask, q.yes_bid_size, q.yes_ask_size) == (0.003, 0.033, 13.06, 40.0)


def test_kalshi_handshake_signature_verifies(tmp_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "k.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    h = kalshi_ws_headers("key-123", path)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2").encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
