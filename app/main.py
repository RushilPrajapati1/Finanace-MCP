"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.errors import register_exception_handlers
from app.api.routers import accounts, balances, health, transactions
from app.config import get_settings

# The MCP server is an optional extra (`pip install -e ".[mcp]"`). When it is not
# installed the REST API still runs; the /mcp transport is simply not mounted.
try:
    from app.mcp.server import mcp as _mcp
except ModuleNotFoundError:  # pragma: no cover - exercised only without the extra
    _mcp = None

DESCRIPTION = """
A double-entry accounting ledger backend for fintech companies.

**Authentication.** Every `/v1/*` endpoint requires an API key, sent as either
`X-API-Key: <key>` or `Authorization: Bearer <key>`. Mint one with
`finledger create-tenant "<name>"`.

**Guarantees.**
* Posted transactions are immutable (enforced by database triggers); corrections
  are made by posting a reversal.
* Every transaction balances per currency (debits == credits).
* Money is stored as integer minor units — never floating point.
"""


def create_app() -> FastAPI:
    settings = get_settings()

    # The streamable-HTTP MCP server runs its own session manager, whose lifespan
    # must run for the duration of the app. Build its ASGI app first (the session
    # manager is created lazily on this call) and drive its lifespan from ours.
    mcp_app = _mcp.streamable_http_app() if _mcp is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if _mcp is not None:
            async with _mcp.session_manager.run():
                yield
        else:  # pragma: no cover - only without the mcp extra
            yield

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # API and MCP responses carry tenant financial data; keep them out of
        # shared caches.
        if request.url.path.startswith("/v1/") or request.url.path.startswith("/mcp"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(health.router)
    app.include_router(accounts.router, prefix="/v1")
    app.include_router(transactions.router, prefix="/v1")
    app.include_router(balances.router, prefix="/v1")

    # Hosted AI clients / backend agents speak MCP here, authenticating per
    # request with X-API-Key / Authorization: Bearer (see app/mcp/auth.py). Not
    # for the browser — the web/ UI keeps calling /v1/...
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    @app.get("/", tags=["health"], include_in_schema=False)
    async def root() -> dict:
        return {"service": settings.app_name, "docs": "/docs"}

    return app


app = create_app()
