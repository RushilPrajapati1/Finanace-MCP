"""Reversals: the only sanctioned way to undo a posted transaction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import AccountType, Direction
from app.domain.errors import AlreadyReversedError
from app.services import accounts as account_service
from app.services import balances as balance_service
from app.services import ledger as ledger_service
from app.services.ledger import PostingInput, TransactionInput


async def _account(session, tenant, type_, name):
    return await account_service.create_account(
        session, tenant.id, name=name, account_type=type_, currency_code="USD"
    )


async def test_reversal_nets_balances_to_zero(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, "cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, "deposits")

    original = await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            postings=[
                PostingInput(cash.id, Direction.DEBIT, Decimal("80.00")),
                PostingInput(deposits.id, Direction.CREDIT, Decimal("80.00")),
            ],
        ),
    )

    reversal = await ledger_service.reverse_transaction(
        session, tenant.id, original.id
    )
    assert reversal.reverses_transaction_id == original.id

    cash_balance = await balance_service.get_account_balance(session, tenant.id, cash.id)
    assert cash_balance.balance == Decimal("0.00")

    tb = await balance_service.trial_balance(session, tenant.id)
    assert tb.balanced is True

    integrity = await balance_service.verify_integrity(session, tenant.id)
    assert integrity["consistent"] is True


async def test_cannot_reverse_twice(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, "cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, "deposits")
    original = await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            postings=[
                PostingInput(cash.id, Direction.DEBIT, Decimal("10.00")),
                PostingInput(deposits.id, Direction.CREDIT, Decimal("10.00")),
            ],
        ),
    )

    await ledger_service.reverse_transaction(session, tenant.id, original.id)
    with pytest.raises(AlreadyReversedError):
        await ledger_service.reverse_transaction(session, tenant.id, original.id)
