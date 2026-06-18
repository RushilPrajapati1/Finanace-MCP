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
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.errors import AuthenticationError
from app.models import ApiKey, Tenant
from app.security import hash_api_key


@dataclass(slots=True)
class McpPrincipal:
    """The authenticated caller plus the audit context for a write tool call."""

    tenant: Tenant
    api_key: ApiKey
    source_ip: str | None


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


def _source_ip_from_request(request) -> str | None:
    """Best-effort request origin for the audit trail (mirrors ``app/api/deps.py``).

    ``X-Forwarded-For`` is client-controlled, so it is only trusted when the
    operator has declared a reverse proxy (``FINLEDGER_TRUST_PROXY_HEADERS``);
    then only the right-most hop is used. Otherwise the TCP peer is used.
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip() or None
    client = getattr(request, "client", None)
    return client.host if client else None


async def _resolve_api_key_and_tenant(
    session: AsyncSession, raw_key: str | None = None
) -> tuple[ApiKey, Tenant]:
    """Resolve a live (non-revoked) API key and its tenant from a raw key."""
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
    return api_key, tenant


async def resolve_tenant(session: AsyncSession, raw_key: str | None = None) -> Tenant:
    """Resolve the calling tenant from an API key (header value or env-var)."""
    _, tenant = await _resolve_api_key_and_tenant(session, raw_key)
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


async def resolve_principal_for_context(session: AsyncSession, ctx) -> McpPrincipal:
    """Resolve the calling principal for a *write* tool invocation.

    Like :func:`resolve_tenant_for_context`, but also returns the API key and
    request origin so the ledger can stamp the audit trail (which credential
    posted, from where) — exactly what the REST router threads through.
    """
    request = getattr(ctx.request_context, "request", None) if ctx is not None else None

    if request is not None:  # streamable-HTTP transport
        raw = _api_key_from_request(request)
        if not raw:
            raise AuthenticationError(
                "missing API key: send X-API-Key or Authorization: Bearer"
            )
        api_key, tenant = await _resolve_api_key_and_tenant(session, raw)
        return McpPrincipal(
            tenant=tenant, api_key=api_key, source_ip=_source_ip_from_request(request)
        )

    # stdio transport: single tenant from the server environment, no HTTP origin
    api_key, tenant = await _resolve_api_key_and_tenant(session)
    return McpPrincipal(tenant=tenant, api_key=api_key, source_ip=None)
