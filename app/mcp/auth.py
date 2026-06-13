"""Tenant resolution for the MCP server via API key."""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import AuthenticationError
from app.models import ApiKey, Tenant
from app.security import hash_api_key


def require_api_key() -> str:
    """Return the configured API key or raise if missing."""
    raw = os.environ.get("FINLEDGER_API_KEY", "").strip()
    if not raw:
        raise AuthenticationError(
            "FINLEDGER_API_KEY is not set. Create a tenant with "
            "`finledger create-tenant` and export the key before starting MCP."
        )
    return raw


async def resolve_tenant(session: AsyncSession, raw_key: str | None = None) -> Tenant:
    """Resolve the calling tenant from an API key."""
    raw = raw_key or require_api_key()
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
