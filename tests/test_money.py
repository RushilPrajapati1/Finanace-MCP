"""Unit tests for exact money handling (no database)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.money import Money, MoneyError, minor_to_decimal


def test_usd_round_trip():
    money = Money.from_decimal("100.00", "usd", exponent=2)
    assert money.minor_units == 10_000
    assert money.currency == "USD"
    assert money.to_decimal(2) == Decimal("100.00")


def test_jpy_has_no_minor_units():
    money = Money.from_decimal("1500", "JPY", exponent=0)
    assert money.minor_units == 1500
    assert money.to_decimal(0) == Decimal("1500")


def test_btc_eight_decimals():
    money = Money.from_decimal("0.00000001", "BTC", exponent=8)
    assert money.minor_units == 1


def test_rejects_sub_minor_precision():
    with pytest.raises(MoneyError):
        Money.from_decimal("1.005", "USD", exponent=2)


def test_rejects_fractional_yen():
    with pytest.raises(MoneyError):
        Money.from_decimal("1.5", "JPY", exponent=0)


def test_rejects_non_finite():
    with pytest.raises(MoneyError):
        Money.from_decimal("NaN", "USD", exponent=2)


def test_rejects_garbage():
    with pytest.raises(MoneyError):
        Money.from_decimal("not-a-number", "USD", exponent=2)


def test_minor_to_decimal_helper():
    assert minor_to_decimal(12345, 2) == Decimal("123.45")
