"""Idempotent transaction posting."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import AccountType, Direction
from app.services import accounts as account_service
from app.services import balances as balance_service
from app.services import ledger as ledger_service
from app.services.ledger import PostingInput, TransactionInput


async def _account(session, tenant, type_, name):
    return await account_service.create_account(
        session, tenant.id, name=name, account_type=type_, currency_code="USD"
    )


async def test_same_idempotency_key_posts_once(session, tenant):
    cash = await _account(session, tenant, AccountType.ASSET, "cash")
    deposits = await _account(session, tenant, AccountType.LIABILITY, "deposits")

    def _input():
        return TransactionInput(
            idempotency_key="deposit-42",
            postings=[
                PostingInput(cash.id, Direction.DEBIT, Decimal("250.00")),
                PostingInput(deposits.id, Direction.CREDIT, Decimal("250.00")),
            ],
        )

    first = await ledger_service.post_transaction(session, tenant.id, _input())
    second = await ledger_service.post_transaction(session, tenant.id, _input())

    assert first.id == second.id  # replay returned the original transaction

    # The money moved exactly once.
    balance = await balance_service.get_account_balance(session, tenant.id, cash.id)
    assert balance.balance == Decimal("250.00")
