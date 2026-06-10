"""Shared FastAPI dependencies: DB session and API-key authentication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.domain.errors import AuthenticationError
from app.models import ApiKey, Tenant
from app.security import hash_api_key

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(slots=True)
class Principal:
    """The authenticated caller plus the audit context for this request."""

    tenant: Tenant
    api_key: ApiKey
    source_ip: str | None


async def _resolve_api_key(
    session: AsyncSession, x_api_key: str | None, authorization: str | None
) -> ApiKey:
    """Resolve a live (non-revoked) API key from the request headers.

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
    return api_key


def _client_ip(request: Request) -> str | None:
    """Best-effort request origin for the audit trail.

    ``X-Forwarded-For`` is client-controlled, so it is only consulted when the
    operator declares a trusted reverse proxy in front of the app
    (``FINLEDGER_TRUST_PROXY_HEADERS=true``) — and then only the right-most hop
    is used, because that is the one appended by the trusted proxy itself; the
    left-most entries can be forged by the caller. Otherwise the TCP peer
    address is used directly."""
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip() or None
    return request.client.host if request.client else None


async def get_principal(
    request: Request,
    session: SessionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the calling tenant, the API key used, and the request origin."""
    api_key = await _resolve_api_key(session, x_api_key, authorization)
    tenant = await session.get(Tenant, api_key.tenant_id)
    if tenant is None:  # pragma: no cover - FK guarantees presence
        raise AuthenticationError("invalid API key")
    return Principal(tenant=tenant, api_key=api_key, source_ip=_client_ip(request))


async def get_tenant(
    session: SessionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Resolve just the calling tenant from an API key."""
    api_key = await _resolve_api_key(session, x_api_key, authorization)
    tenant = await session.get(Tenant, api_key.tenant_id)
    if tenant is None:  # pragma: no cover - FK guarantees presence
        raise AuthenticationError("invalid API key")
    return tenant


TenantDep = Annotated[Tenant, Depends(get_tenant)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
