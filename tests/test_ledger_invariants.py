"""Service-level tests for the core double-entry invariants."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.domain.enums import AccountType, Direction
from app.domain.errors import (
    CurrencyMismatchError,
    InactiveAccountError,
    UnbalancedTransactionError,
    ValidationError,
)
from app.services import accounts as account_service
from app.services import balances as balance_service
from app.services import ledger as ledger_service
from app.services.ledger import PostingInput, TransactionInput


async def _account(session, tenant, type_, currency="USD", name="acct"):
    return await account_service.create_account(
        session, tenant.id, name=name, account_type=type_, currency_code=currency
    )


async def test_balanced_transaction_updates_balances(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")

    await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            description="customer deposit",
            postings=[
                PostingInput(cash.id, Direction.DEBIT, Decimal("100.00")),
                PostingInput(deposits.id, Direction.CREDIT, Decimal("100.00")),
            ],
        ),
    )

    cash_balance = await balance_service.get_account_balance(session, tenant.id, cash.id)
    deposit_balance = await balance_service.get_account_balance(
        session, tenant.id, deposits.id
    )
    # Asset (debit-normal) grows on a debit; liability (credit-normal) grows on a credit.
    assert cash_balance.balance == Decimal("100.00")
    assert deposit_balance.balance == Decimal("100.00")


async def test_unbalanced_transaction_is_rejected(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")

    with pytest.raises(UnbalancedTransactionError):
        await ledger_service.post_transaction(
            session,
            tenant.id,
            TransactionInput(
                postings=[
                    PostingInput(cash.id, Direction.DEBIT, Decimal("100.00")),
                    PostingInput(deposits.id, Direction.CREDIT, Decimal("99.99")),
                ],
            ),
        )


async def test_single_posting_is_rejected(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    with pytest.raises(ValidationError):
        await ledger_service.post_transaction(
            session,
            tenant.id,
            TransactionInput(
                postings=[PostingInput(cash.id, Direction.DEBIT, Decimal("1.00"))]
            ),
        )


async def test_currency_mismatch_is_rejected(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")
    with pytest.raises(CurrencyMismatchError):
        await ledger_service.post_transaction(
            session,
            tenant.id,
            TransactionInput(
                postings=[
                    PostingInput(cash.id, Direction.DEBIT, Decimal("1.00"), currency="EUR"),
                    PostingInput(deposits.id, Direction.CREDIT, Decimal("1.00")),
                ],
            ),
        )


async def test_sub_minor_amount_is_rejected(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")
    with pytest.raises(ValidationError):
        await ledger_service.post_transaction(
            session,
            tenant.id,
            TransactionInput(
                postings=[
                    PostingInput(cash.id, Direction.DEBIT, Decimal("1.005")),
                    PostingInput(deposits.id, Direction.CREDIT, Decimal("1.005")),
                ],
            ),
        )


async def test_multi_currency_balances_per_currency(session, tenant):
    # An FX trade: pay out USD, receive EUR, routed through an FX clearing
    # account in each currency so every currency balances independently.
    usd_cash = await _account(session, tenant, AccountType.ASSET, "USD", "usd_cash")
    usd_fx = await _account(session, tenant, AccountType.EQUITY, "USD", "usd_fx")
    eur_cash = await _account(session, tenant, AccountType.ASSET, "EUR", "eur_cash")
    eur_fx = await _account(session, tenant, AccountType.EQUITY, "EUR", "eur_fx")

    await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            description="sell 110 USD for 100 EUR",
            postings=[
                PostingInput(usd_fx.id, Direction.DEBIT, Decimal("110.00")),
                PostingInput(usd_cash.id, Direction.CREDIT, Decimal("110.00")),
                PostingInput(eur_cash.id, Direction.DEBIT, Decimal("100.00")),
                PostingInput(eur_fx.id, Direction.CREDIT, Decimal("100.00")),
            ],
        ),
    )

    tb = await balance_service.trial_balance(session, tenant.id)
    assert tb.balanced is True
    by_currency = {c.currency: c for c in tb.currencies}
    assert by_currency["USD"].balanced
    assert by_currency["EUR"].balanced


async def test_inactive_account_cannot_receive_postings(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")
    cash.is_active = False
    await session.commit()

    with pytest.raises(InactiveAccountError):
        await ledger_service.post_transaction(
            session,
            tenant.id,
            TransactionInput(
                postings=[
                    PostingInput(cash.id, Direction.DEBIT, Decimal("1.00")),
                    PostingInput(deposits.id, Direction.CREDIT, Decimal("1.00")),
                ],
            ),
        )


async def test_postings_are_immutable(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, name="cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, name="deposits")
    await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            postings=[
                PostingInput(cash.id, Direction.DEBIT, Decimal("5.00")),
                PostingInput(deposits.id, Direction.CREDIT, Decimal("5.00")),
            ],
        ),
    )

    # The append-only trigger must reject any attempt to rewrite history.
    with pytest.raises(DBAPIError):
        await session.execute(text("UPDATE postings SET amount = 1"))
    await session.rollback()

    with pytest.raises(DBAPIError):
        await session.execute(text("DELETE FROM transactions"))
    await session.rollback()
