"""Transaction endpoints: post, fetch, list, and reverse."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import SessionDep, TenantDep
from app.api.schemas import (
    ReversalCreate,
    TransactionCreate,
    TransactionOut,
)
from app.models import Currency, Transaction
from app.services import ledger as ledger_service
from app.services.ledger import PostingInput, TransactionInput

router = APIRouter(prefix="/transactions", tags=["transactions"])


async def _exponents_for(session: AsyncSession, transaction: Transaction) -> dict[str, int]:
    codes = {p.currency_code for p in transaction.postings}
    if not codes:
        return {}
    rows = await session.scalars(select(Currency).where(Currency.code.in_(codes)))
    return {c.code: c.exponent for c in rows}


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
async def post_transaction(
    body: TransactionCreate, tenant: TenantDep, session: SessionDep
) -> TransactionOut:
    transaction = await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            postings=[
                PostingInput(
                    account_id=p.account_id,
                    direction=p.direction,
                    amount=p.amount,
                    currency=p.currency,
                )
                for p in body.postings
            ],
            description=body.description,
            idempotency_key=body.idempotency_key,
            external_id=body.external_id,
            meta=body.metadata,
        ),
    )
    return TransactionOut.from_model(
        transaction, await _exponents_for(session, transaction)
    )


@router.get("", response_model=list[TransactionOut])
async def list_transactions(
    tenant: TenantDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[TransactionOut]:
    transactions = (
        await session.scalars(
            select(Transaction)
            .where(Transaction.tenant_id == tenant.id)
            .options(selectinload(Transaction.postings))
            .order_by(Transaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    codes: set[str] = set()
    for t in transactions:
        codes.update(p.currency_code for p in t.postings)
    exponents = (
        {
            c.code: c.exponent
            for c in await session.scalars(
                select(Currency).where(Currency.code.in_(codes))
            )
        }
        if codes
        else {}
    )
    return [TransactionOut.from_model(t, exponents) for t in transactions]


@router.get("/{transaction_id}", response_model=TransactionOut)
async def get_transaction(
    transaction_id: UUID, tenant: TenantDep, session: SessionDep
) -> TransactionOut:
    transaction = await ledger_service.get_transaction(
        session, tenant.id, transaction_id
    )
    return TransactionOut.from_model(
        transaction, await _exponents_for(session, transaction)
    )


@router.post(
    "/{transaction_id}/reversal",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
)
async def reverse_transaction(
    transaction_id: UUID,
    body: ReversalCreate,
    tenant: TenantDep,
    session: SessionDep,
) -> TransactionOut:
    reversal = await ledger_service.reverse_transaction(
        session,
        tenant.id,
        transaction_id,
        idempotency_key=body.idempotency_key,
        description=body.description,
    )
    return TransactionOut.from_model(
        reversal, await _exponents_for(session, reversal)
    )
