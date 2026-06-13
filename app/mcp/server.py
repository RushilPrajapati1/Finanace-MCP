"""FinLedger MCP server — exposes ledger data and analysis to AI assistants.

Run (stdio, for Cursor / Claude Desktop):
    python -m app.mcp

Run with MCP Inspector (dev):
    mcp dev app/mcp/server.py

Requires:
    FINLEDGER_DATABASE_URL  — Postgres connection string
    FINLEDGER_API_KEY       — tenant API key from `finledger create-tenant`
"""

from __future__ import annotations

import uuid

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.domain.errors import LedgerError
from app.mcp.auth import resolve_tenant
from app.mcp.portfolio import portfolio_summary
from app.mcp.serializers import (
    account_dict,
    balance_dict,
    statement_dict,
    transaction_dict,
    trial_balance_dict,
)
from app.models import Currency, Transaction
from app.services import accounts as account_service
from app.services import balances as balance_service

mcp = FastMCP(
    "FinLedger",
    instructions=(
        "Double-entry accounting ledger. Use these tools to query balances, "
        "transaction history, trial balance, portfolio rollups (net worth and "
        "P&L), and ledger integrity. All amounts are decimal strings."
    ),
)


async def _with_session(fn):
    """Open a DB session, resolve the tenant, and run ``fn(session, tenant)``."""
    async with SessionLocal() as session:
        try:
            tenant = await resolve_tenant(session)
            return await fn(session, tenant)
        except LedgerError as exc:
            return {"error": exc.__class__.__name__, "message": str(exc)}


@mcp.tool()
async def list_accounts(limit: int = 50) -> dict:
    """List chart-of-accounts entries for the authenticated tenant."""
    limit = max(1, min(limit, 200))

    async def run(session: AsyncSession, tenant):
        accounts = await account_service.list_accounts(
            session, tenant.id, limit=limit
        )
        return {"accounts": [account_dict(a) for a in accounts]}

    return await _with_session(run)


@mcp.tool()
async def get_account_balance(account_id: str) -> dict:
    """Get the current balance for one account by UUID."""
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {"error": "ValidationError", "message": f"invalid account_id: {account_id!r}"}

    async def run(session: AsyncSession, tenant):
        view = await balance_service.get_account_balance(session, tenant.id, aid)
        return balance_dict(view)

    return await _with_session(run)


@mcp.tool()
async def get_account_statement(
    account_id: str, limit: int = 25, offset: int = 0
) -> dict:
    """Get chronological postings for an account with running balances."""
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {"error": "ValidationError", "message": f"invalid account_id: {account_id!r}"}

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    async def run(session: AsyncSession, tenant):
        entries = await balance_service.account_statement(
            session, tenant.id, aid, limit=limit, offset=offset
        )
        return {"entries": [statement_dict(e) for e in entries]}

    return await _with_session(run)


@mcp.tool()
async def list_transactions(limit: int = 25, offset: int = 0) -> dict:
    """List recent journal transactions with their postings."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    async def run(session: AsyncSession, tenant):
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
        for txn in transactions:
            codes.update(p.currency_code for p in txn.postings)
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
        return {
            "transactions": [
                transaction_dict(txn, exponents) for txn in transactions
            ]
        }

    return await _with_session(run)


@mcp.tool()
async def get_trial_balance() -> dict:
    """Return per-currency debit/credit totals. A healthy ledger balances to zero."""
    async def run(session: AsyncSession, tenant):
        trial = await balance_service.trial_balance(session, tenant.id)
        return trial_balance_dict(trial)

    return await _with_session(run)


@mcp.tool()
async def verify_ledger_integrity() -> dict:
    """Recompute balances from postings and detect drift from materialised totals."""
    async def run(session: AsyncSession, tenant):
        return await balance_service.verify_integrity(session, tenant.id)

    return await _with_session(run)


@mcp.tool()
async def get_portfolio_summary() -> dict:
    """Roll up net worth (assets − liabilities) and P&L (revenue − expenses) by currency."""
    async def run(session: AsyncSession, tenant):
        rows = await portfolio_summary(session, tenant.id)
        return {
            "currencies": [
                {
                    "currency": row.currency,
                    "assets": format(row.assets, "f"),
                    "liabilities": format(row.liabilities, "f"),
                    "revenue": format(row.revenue, "f"),
                    "expense": format(row.expense, "f"),
                    "net_worth": format(row.net_worth, "f"),
                    "profit_and_loss": format(row.profit_and_loss, "f"),
                }
                for row in rows
            ]
        }

    return await _with_session(run)


@mcp.prompt()
def analyze_finances() -> str:
    """Prompt template for a full financial health review."""
    return (
        "Review this tenant's finances using the FinLedger MCP tools. "
        "1) Call get_portfolio_summary for net worth and P&L by currency. "
        "2) Call get_trial_balance to confirm debits equal credits. "
        "3) Call verify_ledger_integrity to check for balance drift. "
        "4) Optionally list recent transactions or account statements for detail. "
        "Summarise findings in plain language and flag any integrity issues."
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
