"""Account (chart-of-accounts) use-cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountType
from app.domain.errors import (
    AccountNotFoundError,
    CurrencyNotFoundError,
    DuplicateAccountError,
)
from app.models import Account, AccountBalance, Currency


async def create_account(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    account_type: AccountType,
    currency_code: str,
    external_id: str | None = None,
    meta: dict | None = None,
) -> Account:
    """Create an account and its (zeroed) balance row.

    Creation is idempotent on ``external_id``: presenting the same external_id
    again returns the existing account rather than erroring, which makes client
    retries safe.
    """
    currency = await session.get(Currency, currency_code.upper())
    if currency is None:
        raise CurrencyNotFoundError(f"currency {currency_code!r} is not registered")

    if external_id is not None:
        existing = await session.scalar(
            select(Account).where(
                Account.tenant_id == tenant_id,
                Account.external_id == external_id,
            )
        )
        if existing is not None:
            return existing

    account = Account(
        tenant_id=tenant_id,
        name=name,
        type=account_type.value,
        currency_code=currency.code,
        external_id=external_id,
        meta=meta,
    )
    session.add(account)
    await session.flush()  # assign account.id

    session.add(
        AccountBalance(
            account_id=account.id,
            tenant_id=tenant_id,
            currency_code=currency.code,
        )
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if external_id is not None:
            existing = await session.scalar(
                select(Account).where(
                    Account.tenant_id == tenant_id,
                    Account.external_id == external_id,
                )
            )
            if existing is not None:
                return existing
        raise DuplicateAccountError() from exc

    await session.refresh(account)
    return account


async def get_account(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    account = await session.scalar(
        select(Account).where(
            Account.id == account_id, Account.tenant_id == tenant_id
        )
    )
    if account is None:
        raise AccountNotFoundError(f"account {account_id} not found")
    return account


async def list_accounts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[Account]:
    result = await session.scalars(
        select(Account)
        .where(Account.tenant_id == tenant_id)
        .order_by(Account.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()
