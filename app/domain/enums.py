"""Account types, posting directions, and the rules that relate them.

In double-entry accounting every account has a *normal balance* — the side
(debit or credit) on which an increase is recorded. The five classical account
types split cleanly into debit-normal and credit-normal:

    assets, expenses                -> debit-normal
    liabilities, equity, revenue    -> credit-normal

This is captured by the accounting equation:

    assets + expenses = liabilities + equity + revenue
"""

from __future__ import annotations

import enum


class AccountType(enum.StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class Direction(enum.StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"

    @property
    def opposite(self) -> Direction:
        return Direction.CREDIT if self is Direction.DEBIT else Direction.DEBIT


_DEBIT_NORMAL = frozenset({AccountType.ASSET, AccountType.EXPENSE})


def normal_balance(account_type: AccountType) -> Direction:
    """Return the side on which `account_type` records an increase."""
    return Direction.DEBIT if account_type in _DEBIT_NORMAL else Direction.CREDIT


def balance_sign(account_type: AccountType, direction: Direction) -> int:
    """+1 if a posting in `direction` increases the account's balance, else -1.

    A debit to a debit-normal account (e.g. cash, an asset) increases it; a
    credit to that same account decreases it. The reverse holds for
    credit-normal accounts.
    """
    return 1 if direction is normal_balance(account_type) else -1
