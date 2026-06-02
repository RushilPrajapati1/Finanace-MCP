"""Ledger-level reporting: trial balance and integrity verification."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import SessionDep, TenantDep
from app.api.schemas import IntegrityOut, TrialBalanceOut
from app.services import balances as balance_service

router = APIRouter(prefix="/ledger", tags=["ledger"])


@router.get("/trial-balance", response_model=TrialBalanceOut)
async def trial_balance(tenant: TenantDep, session: SessionDep) -> TrialBalanceOut:
    """Per-currency debit/credit totals. ``balanced`` must be true for a
    healthy ledger."""
    tb = await balance_service.trial_balance(session, tenant.id)
    return TrialBalanceOut.from_model(tb)


@router.get("/verify", response_model=IntegrityOut)
async def verify_integrity(tenant: TenantDep, session: SessionDep) -> IntegrityOut:
    """Recompute balances from the immutable posting history and report any
    drift from the materialised balances."""
    result = await balance_service.verify_integrity(session, tenant.id)
    return IntegrityOut(**result)
