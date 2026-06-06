"""Account endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.api.deps import SessionDep, TenantDep
from app.api.schemas import (
    AccountCreate,
    AccountOut,
    BalanceOut,
    StatementEntryOut,
)
from app.services import accounts as account_service
from app.services import balances as balance_service

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate, tenant: TenantDep, session: SessionDep
) -> AccountOut:
    account = await account_service.create_account(
        session,
        tenant.id,
        name=body.name,
        account_type=body.type,
        currency_code=body.currency,
        external_id=body.external_id,
        meta=body.metadata,
    )
    return AccountOut.from_model(account)


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    tenant: TenantDep,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[AccountOut]:
    accounts = await account_service.list_accounts(
        session, tenant.id, limit=limit, offset=offset
    )
    return [AccountOut.from_model(a) for a in accounts]


@router.get("/{account_id}", response_model=AccountOut)
async def get_account(
    account_id: UUID, tenant: TenantDep, session: SessionDep
) -> AccountOut:
    account = await account_service.get_account(session, tenant.id, account_id)
    return AccountOut.from_model(account)


@router.get("/{account_id}/balance", response_model=BalanceOut)
async def get_account_balance(
    account_id: UUID, tenant: TenantDep, session: SessionDep
) -> BalanceOut:
    view = await balance_service.get_account_balance(session, tenant.id, account_id)
    return BalanceOut.from_view(view)


@router.get("/{account_id}/statement", response_model=list[StatementEntryOut])
async def get_account_statement(
    account_id: UUID,
    tenant: TenantDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[StatementEntryOut]:
    """Chronological postings for an account, each with the running balance it
    produced — a ready-to-render statement."""
    entries = await balance_service.account_statement(
        session, tenant.id, account_id, limit=limit, offset=offset
    )
    return [StatementEntryOut.from_entry(e) for e in entries]
