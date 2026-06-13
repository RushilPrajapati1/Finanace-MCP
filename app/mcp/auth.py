"""Tenant resolution for the MCP server.

Two transports, two auth sources:

* **stdio** (local desktop: Claude Desktop, Cursor) — a single tenant resolved
  from the ``FINLEDGER_API_KEY`` env var.
* **streamable HTTP** (hosted AI clients / backend agents) — a *per-request*
  API key read from the ``X-API-Key`` / ``Authorization: Bearer`` header, so one
  server serves many isolated tenants. Mirrors the REST API's auth exactly.
"""

from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import AuthenticationError
from app.models import ApiKey, Tenant
from app.security import hash_api_key


def require_api_key() -> str:
    """Return the env-var API key (stdio transport) or raise if missing."""
    raw = os.environ.get("FINLEDGER_API_KEY", "").strip()
    if not raw:
        raise AuthenticationError(
            "FINLEDGER_API_KEY is not set. Create a tenant with "
            "`finledger create-tenant` and export the key before starting MCP."
        )
    return raw


def _api_key_from_request(request) -> str | None:
    """Extract an API key from HTTP headers (mirrors ``app/api/deps.py``)."""
    raw = request.headers.get("x-api-key")
    if raw and raw.strip():
        return raw.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    return None


async def resolve_tenant(session: AsyncSession, raw_key: str | None = None) -> Tenant:
    """Resolve the calling tenant from an API key (header value or env-var)."""
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


async def resolve_tenant_for_context(session: AsyncSession, ctx) -> Tenant:
    """Resolve the tenant for an MCP tool invocation, picking the auth source
    from the transport.

    Over **streamable HTTP** the per-request ``Context`` carries the underlying
    HTTP request, so the API key comes from its headers — a missing key is a
    hard error (the server's own env-var key must never stand in for an
    unauthenticated HTTP caller). Over **stdio** there is no HTTP request, so we
    fall back to ``FINLEDGER_API_KEY``.
    """
    request = getattr(ctx.request_context, "request", None) if ctx is not None else None

    if request is not None:  # streamable-HTTP transport
        raw = _api_key_from_request(request)
        if not raw:
            raise AuthenticationError(
                "missing API key: send X-API-Key or Authorization: Bearer"
            )
        return await resolve_tenant(session, raw)

    # stdio transport: single tenant from the server environment
    return await resolve_tenant(session)
