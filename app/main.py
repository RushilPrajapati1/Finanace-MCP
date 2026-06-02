"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.routers import accounts, balances, health, transactions
from app.config import get_settings

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
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
    )
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(accounts.router, prefix="/v1")
    app.include_router(transactions.router, prefix="/v1")
    app.include_router(balances.router, prefix="/v1")

    @app.get("/", tags=["health"], include_in_schema=False)
    async def root() -> dict:
        return {"service": settings.app_name, "docs": "/docs"}

    return app


app = create_app()
