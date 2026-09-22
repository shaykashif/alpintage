from decimal import Decimal

from kalshi_engine.fees import taker_fee, total_taker_fee


def test_single_contract_rounds_up():
    assert taker_fee(1, "0.50") == Decimal("0.02")  # 0.0175 -> 0.02


def test_hundred_contracts_mid_price():
    assert taker_fee(100, "0.50") == Decimal("1.75")


def test_hundred_contracts_low_price():
    assert taker_fee(100, "0.10") == Decimal("0.63")


def test_total_taker_fee_sums_legs():
    total = total_taker_fee([(1, "0.50"), (1, "0.50")])
    assert total == taker_fee(1, "0.50") * 2
