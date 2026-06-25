"""Account (chart-of-accounts) use-cases."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountType, Direction, normal_balance
from app.domain.errors import (
    AccountNotEmptyError,
    AccountNotFoundError,
    CurrencyNotFoundError,
    DuplicateAccountError,
    LedgerError,
    ValidationError,
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


async def get_account_by_external_id(
    session: AsyncSession, tenant_id: uuid.UUID, external_id: str
) -> Account:
    account = await session.scalar(
        select(Account).where(
            Account.external_id == external_id, Account.tenant_id == tenant_id
        )
    )
    if account is None:
        raise AccountNotFoundError(
            f"account with external_id {external_id!r} not found"
        )
    return account


async def update_account(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str | None = None,
    external_id: str | None = None,
) -> Account:
    """Update mutable metadata (``name``, ``external_id``) on an account.

    ``type`` and ``currency`` are deliberately *not* updatable: changing them
    would corrupt historical reports, so they are fixed at creation. A new
    ``external_id`` must stay unique within the tenant.
    """
    account = await get_account(session, tenant_id, account_id)

    if name is not None:
        account.name = name

    if external_id is not None and external_id != account.external_id:
        clash = await session.scalar(
            select(Account).where(
                Account.tenant_id == tenant_id,
                Account.external_id == external_id,
                Account.id != account_id,
            )
        )
        if clash is not None:
            raise DuplicateAccountError(
                f"external_id {external_id!r} is already in use by another account"
            )
        account.external_id = external_id

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise DuplicateAccountError() from exc

    await session.refresh(account)
    return account


async def deactivate_account(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> Account:
    """Soft-close an account so it can no longer receive postings.

    History is preserved (this is never a hard delete); the account keeps
    appearing in statements and reports. Rejected if the account still holds a
    non-zero balance — zero it out with a transfer first.
    """
    account = await get_account(session, tenant_id, account_id)

    balance = await session.get(AccountBalance, account_id)
    if balance is not None:
        debits, credits = int(balance.posted_debits), int(balance.posted_credits)
        if normal_balance(AccountType(account.type)) is Direction.DEBIT:
            signed = debits - credits
        else:
            signed = credits - debits
        if signed != 0:
            raise AccountNotEmptyError(
                f"account {account_id} has a non-zero balance; zero it out before "
                "deactivating"
            )

    account.is_active = False
    await session.commit()
    await session.refresh(account)
    return account


@dataclass(slots=True)
class ImportResult:
    index: int
    external_id: str | None
    status: str  # 'created' | 'skipped' | 'error'
    account: Account | None = None
    error: dict | None = None


async def import_accounts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rows: list[dict],
    *,
    skip_existing: bool = True,
) -> list[ImportResult]:
    """Create many accounts in one call, reporting a result per row.

    Per-row best-effort: a bad row is recorded as ``error`` and the rest still
    proceed. Each row is ``{name, type, currency, external_id?}``. When
    ``skip_existing`` (default), a row whose ``external_id`` already exists is
    reported as ``skipped``; otherwise it is an ``error`` (duplicate).
    """
    results: list[ImportResult] = []
    for index, row in enumerate(rows):
        external_id = row.get("external_id")
        try:
            name = row.get("name")
            if not name:
                raise ValidationError("row is missing 'name'")
            try:
                atype = AccountType(row["type"])
            except (KeyError, ValueError):
                raise ValidationError(
                    f"invalid or missing 'type': {row.get('type')!r}; use one of "
                    f"{[t.value for t in AccountType]}"
                ) from None
            currency = row.get("currency")
            if not currency:
                raise ValidationError("row is missing 'currency'")

            if external_id is not None:
                existing = await session.scalar(
                    select(Account).where(
                        Account.tenant_id == tenant_id,
                        Account.external_id == external_id,
                    )
                )
                if existing is not None:
                    if skip_existing:
                        results.append(
                            ImportResult(
                                index=index,
                                external_id=external_id,
                                status="skipped",
                                account=existing,
                            )
                        )
                        continue
                    raise DuplicateAccountError(
                        f"external_id {external_id!r} already exists"
                    )

            account = await create_account(
                session,
                tenant_id,
                name=name,
                account_type=atype,
                currency_code=currency,
                external_id=external_id,
            )
            results.append(
                ImportResult(
                    index=index,
                    external_id=external_id,
                    status="created",
                    account=account,
                )
            )
        except LedgerError as exc:
            results.append(
                ImportResult(
                    index=index,
                    external_id=external_id,
                    status="error",
                    error={"code": exc.code, "message": str(exc)},
                )
            )
    return results


async def list_accounts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    limit: int = 100,
    offset: int = 0,
    include_inactive: bool = False,
) -> Sequence[Account]:
    stmt = select(Account).where(Account.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(Account.is_active.is_(True))
    result = await session.scalars(
        stmt.order_by(Account.created_at.desc()).limit(limit).offset(offset)
    )
    return result.all()
