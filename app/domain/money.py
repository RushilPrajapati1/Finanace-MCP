"""Exact money handling.

A ledger must never lose a cent to binary floating point. Every amount in the
system is stored and manipulated as an integer count of a currency's *minor
units* (cents for USD, satoshi for BTC, and so on). The currency's ``exponent``
says how many decimal places map onto those minor units:

    USD exponent 2  ->  $1.00   == 100 minor units
    JPY exponent 0  ->  ¥1      ==   1 minor unit
    BTC exponent 8  ->  ₿0.00000001 == 1 minor unit

`Money` is the boundary type: it parses human/decimal input into integer minor
units and refuses any amount that cannot be represented exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class MoneyError(ValueError):
    """Raised when an amount cannot be represented exactly in a currency."""


@dataclass(frozen=True, slots=True)
class Money:
    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor_units, int) or isinstance(self.minor_units, bool):
            raise MoneyError("minor_units must be an int")

    @classmethod
    def from_decimal(
        cls, amount: Decimal | str | int, currency: str, exponent: int
    ) -> Money:
        """Build `Money` from a decimal amount, rejecting sub-minor precision."""
        if exponent < 0:
            raise MoneyError("currency exponent must be non-negative")
        try:
            value = Decimal(amount)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise MoneyError(f"invalid amount: {amount!r}") from exc
        if not value.is_finite():
            raise MoneyError("amount must be a finite number")

        scaled = value * (Decimal(10) ** exponent)
        if scaled != scaled.to_integral_value():
            raise MoneyError(
                f"amount {value} is finer than {currency.upper()} permits "
                f"({exponent} decimal places)"
            )
        return cls(minor_units=int(scaled), currency=currency.upper())

    def to_decimal(self, exponent: int) -> Decimal:
        """Render minor units back into a decimal amount."""
        return Decimal(self.minor_units).scaleb(-exponent)


def minor_to_decimal(minor_units: int, exponent: int) -> Decimal:
    """Convenience wrapper to format raw minor units as a decimal."""
    return Decimal(int(minor_units)).scaleb(-exponent)
