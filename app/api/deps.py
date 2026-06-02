"""Shared FastAPI dependencies: DB session and API-key authentication."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import AuthenticationError
from app.models import ApiKey, Tenant
from app.security import hash_api_key

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_tenant(
    session: SessionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Resolve the calling tenant from an API key.

    Accepts either ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``.
    """
    raw = x_api_key
    if not raw and authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    if not raw:
        raise AuthenticationError("missing API key")

    api_key = await session.scalar(
        select(ApiKey).where(
            ApiKey.key_hash == hash_api_key(raw),
            ApiKey.revoked_at.is_(None),
        )
    )
    if api_key is None:
        raise AuthenticationError("invalid or revoked API key")

    tenant = await session.get(Tenant, api_key.tenant_id)
    if tenant is None:  # pragma: no cover - FK guarantees presence
        raise AuthenticationError("invalid API key")
    return tenant


TenantDep = Annotated[Tenant, Depends(get_tenant)]
