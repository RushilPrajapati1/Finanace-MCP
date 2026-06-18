"""FinLedger MCP server — exposes ledger data and analysis to AI assistants.

Two transports share this one server and tool set:

* **stdio** (local desktop: Cursor / Claude Desktop) — single tenant from the
  ``FINLEDGER_API_KEY`` env var::

      python -m app.mcp                 # or: mcp dev app/mcp/server.py

* **streamable HTTP** (hosted AI clients / backend agents) — mounted by the
  FastAPI app at ``/mcp`` (see ``app/main.py``); each request authenticates with
  its own ``X-API-Key`` / ``Authorization: Bearer`` header, so one server serves
  many tenants. This is *not* for the browser — the ``web/`` UI keeps calling
  ``/v1/...``.

Requires:
    FINLEDGER_DATABASE_URL  — Postgres connection string
    FINLEDGER_API_KEY       — tenant API key (stdio transport only)
"""

from __future__ import annotations

import hashlib
import json
import uuid

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db import SessionLocal
from app.domain.enums import AccountType, Direction
from app.domain.errors import LedgerError, ValidationError
from app.mcp.auth import resolve_principal_for_context, resolve_tenant_for_context
from app.mcp.portfolio import portfolio_summary
from app.mcp.serializers import (
    account_dict,
    balance_dict,
    statement_dict,
    transaction_dict,
    transaction_preview_dict,
    trial_balance_dict,
)
from app.models import Currency, Transaction
from app.services import accounts as account_service
from app.services import balances as balance_service
from app.services import ledger as ledger_service
from app.services.ledger import PostingInput, TransactionInput

def _transport_security() -> TransportSecuritySettings | None:
    """Build DNS-rebinding protection for the HTTP transport from config.

    Returns ``None`` (SDK default: localhost only) unless the operator has
    declared the public host/origin, which is required once the server is
    reachable at a real domain or MCP requests are rejected with 421.
    """
    settings = get_settings()
    hosts = settings.mcp_allowed_hosts_list
    origins = settings.mcp_allowed_origins_list
    if not hosts and not origins:
        return None
    return TransportSecuritySettings(
        allowed_hosts=hosts or ["127.0.0.1:*", "localhost:*"],
        allowed_origins=origins or ["http://127.0.0.1:*", "http://localhost:*"],
    )


mcp = FastMCP(
    "FinLedger",
    instructions=(
        "Double-entry accounting ledger. Use these tools to query balances, "
        "transaction history, trial balance, portfolio rollups (net worth and "
        "P&L), and ledger integrity. All amounts are decimal strings."
    ),
    # The FastAPI app mounts this server's ASGI app at /mcp, so the streamable
    # endpoint lives at the mount root ("/") within the sub-app -> /mcp on the API.
    streamable_http_path="/",
    # Reply with plain JSON instead of an SSE stream. Our tools are all
    # request/response (no server streaming), so this loses nothing and lets
    # simpler clients connect: JSON mode only requires `Accept: application/json`
    # rather than `application/json, text/event-stream`. Full MCP clients still
    # work (they accept both).
    json_response=True,
    transport_security=_transport_security(),
)


async def _with_session(ctx: Context, fn):
    """Open a DB session, resolve the calling tenant from the request context
    (HTTP header) or the env var (stdio), and run ``fn(session, tenant)``."""
    async with SessionLocal() as session:
        try:
            tenant = await resolve_tenant_for_context(session, ctx)
            return await fn(session, tenant)
        except LedgerError as exc:
            return {"error": exc.code, "message": str(exc)}


async def _with_write_session(ctx: Context, fn):
    """Like :func:`_with_session`, but resolves the full principal (tenant + API
    key + origin) so write tools can stamp the audit trail, and surfaces the
    domain error's stable ``code`` for agents to branch on."""
    async with SessionLocal() as session:
        try:
            principal = await resolve_principal_for_context(session, ctx)
            return await fn(session, principal)
        except LedgerError as exc:
            return {"error": exc.code, "message": str(exc)}


async def _exponents_for_transaction(
    session: AsyncSession, transaction: Transaction
) -> dict[str, int]:
    codes = {p.currency_code for p in transaction.postings}
    if not codes:
        return {}
    return {
        c.code: c.exponent
        for c in await session.scalars(select(Currency).where(Currency.code.in_(codes)))
    }


class PostingArg(BaseModel):
    """One leg of a transaction. Amounts are decimal **strings** (e.g. "150.00")
    to avoid float precision loss — never JSON numbers."""

    account_id: str = Field(description="UUID of the account to post against")
    direction: str = Field(description="'debit' or 'credit'")
    amount: str = Field(description="positive decimal amount as a string, e.g. '150.00'")
    currency: str | None = Field(
        default=None,
        description="optional ISO code; if given it must match the account's currency",
    )


def _to_posting_inputs(postings: list[PostingArg]) -> list[PostingInput]:
    """Map the agent-facing posting args to service-layer ``PostingInput``s,
    raising ``ValidationError`` (a ``LedgerError``) on malformed input so the
    caller gets a structured error rather than a 500."""
    inputs: list[PostingInput] = []
    for p in postings:
        try:
            account_id = uuid.UUID(p.account_id)
        except (ValueError, AttributeError):
            raise ValidationError(f"invalid account_id: {p.account_id!r}")
        try:
            direction = Direction(p.direction)
        except ValueError:
            raise ValidationError(
                f"invalid direction {p.direction!r}: use 'debit' or 'credit'"
            )
        # ``amount`` stays a string here; the posting engine parses it exactly via
        # Money.from_decimal and rejects sub-minor precision.
        inputs.append(
            PostingInput(
                account_id=account_id,
                direction=direction,
                amount=p.amount,
                currency=p.currency,
            )
        )
    return inputs


def _derive_idempotency_key(description: str | None, postings: list[PostingArg]) -> str:
    """Deterministic fallback key from the transaction's content.

    Agents retry. When the caller does not supply an ``idempotency_key`` we hash
    the content so a retried identical call collapses to one post instead of
    double-posting. The trade-off (documented for the agent): two *intentionally*
    identical transactions also collapse — pass an explicit key to distinguish
    them.
    """
    payload = {
        "description": description,
        "postings": sorted(
            (p.account_id, p.direction, p.amount, p.currency or "") for p in postings
        ),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "mcp-" + hashlib.sha256(blob.encode()).hexdigest()


@mcp.tool()
async def list_accounts(ctx: Context, limit: int = 50) -> dict:
    """List chart-of-accounts entries for the authenticated tenant."""
    limit = max(1, min(limit, 200))

    async def run(session: AsyncSession, tenant):
        accounts = await account_service.list_accounts(
            session, tenant.id, limit=limit
        )
        return {"accounts": [account_dict(a) for a in accounts]}

    return await _with_session(ctx, run)


@mcp.tool()
async def get_account_balance(ctx: Context, account_id: str) -> dict:
    """Get the current balance for one account by UUID."""
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {"error": "validation_error", "message": f"invalid account_id: {account_id!r}"}

    async def run(session: AsyncSession, tenant):
        view = await balance_service.get_account_balance(session, tenant.id, aid)
        return balance_dict(view)

    return await _with_session(ctx, run)


@mcp.tool()
async def get_account_statement(
    ctx: Context, account_id: str, limit: int = 25, offset: int = 0
) -> dict:
    """Get chronological postings for an account with running balances."""
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {"error": "validation_error", "message": f"invalid account_id: {account_id!r}"}

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    async def run(session: AsyncSession, tenant):
        entries = await balance_service.account_statement(
            session, tenant.id, aid, limit=limit, offset=offset
        )
        return {"entries": [statement_dict(e) for e in entries]}

    return await _with_session(ctx, run)


@mcp.tool()
async def list_transactions(ctx: Context, limit: int = 25, offset: int = 0) -> dict:
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

    return await _with_session(ctx, run)


@mcp.tool()
async def get_trial_balance(ctx: Context) -> dict:
    """Return per-currency debit/credit totals. A healthy ledger balances to zero."""
    async def run(session: AsyncSession, tenant):
        trial = await balance_service.trial_balance(session, tenant.id)
        return trial_balance_dict(trial)

    return await _with_session(ctx, run)


@mcp.tool()
async def verify_ledger_integrity(ctx: Context) -> dict:
    """Recompute balances from postings and detect drift from materialised totals."""
    async def run(session: AsyncSession, tenant):
        return await balance_service.verify_integrity(session, tenant.id)

    return await _with_session(ctx, run)


@mcp.tool()
async def get_portfolio_summary(ctx: Context) -> dict:
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

    return await _with_session(ctx, run)


@mcp.tool()
async def create_account(
    ctx: Context,
    name: str,
    account_type: str,
    currency: str,
    external_id: str | None = None,
) -> dict:
    """Create a chart-of-accounts entry for the authenticated tenant.

    ``account_type`` is one of: asset, liability, equity, revenue, expense.
    Creation is idempotent on ``external_id`` — re-creating with the same one
    returns the existing account instead of erroring.
    """
    try:
        atype = AccountType(account_type)
    except ValueError:
        return {
            "error": "validation_error",
            "message": (
                f"invalid account_type {account_type!r}: use one of "
                f"{[t.value for t in AccountType]}"
            ),
        }

    async def run(session: AsyncSession, principal):
        account = await account_service.create_account(
            session,
            principal.tenant.id,
            name=name,
            account_type=atype,
            currency_code=currency,
            external_id=external_id,
        )
        return account_dict(account)

    return await _with_write_session(ctx, run)


@mcp.tool()
async def post_transaction(
    ctx: Context,
    description: str,
    postings: list[PostingArg],
    idempotency_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Post a balanced double-entry transaction.

    ``postings`` needs at least two legs and, within each currency, debits must
    equal credits — the engine rejects anything else, so this cannot create
    money out of nothing. Pass an ``idempotency_key`` so retries are safe; if you
    omit one, a key is derived from the transaction's content so an accidental
    retry won't double-post (two intentionally identical transactions need
    distinct keys). To undo a transaction, post a reversal — never edit.

    Set ``dry_run=True`` to validate and see the projected balance impact
    *without committing* — the same validation runs, so a clean dry-run
    guarantees the real post will pass. Use it to confirm before writing.
    """
    async def run(session: AsyncSession, principal):
        data = TransactionInput(
            postings=_to_posting_inputs(postings),
            description=description,
            idempotency_key=idempotency_key
            or _derive_idempotency_key(description, postings),
            actor="mcp-agent",
        )
        if dry_run:
            preview = await ledger_service.preview_transaction(
                session, principal.tenant.id, data
            )
            return transaction_preview_dict(preview)

        transaction = await ledger_service.post_transaction(
            session,
            principal.tenant.id,
            data,
            api_key_id=principal.api_key.id,
            source_ip=principal.source_ip,
        )
        return transaction_dict(
            transaction, await _exponents_for_transaction(session, transaction)
        )

    return await _with_write_session(ctx, run)


@mcp.tool()
async def reverse_transaction(
    ctx: Context,
    transaction_id: str,
    description: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Reverse a posted transaction by posting its mirror image.

    Corrections are made by reversal, never by editing or deleting — posted
    transactions are immutable. A transaction can be reversed at most once.
    """
    async def run(session: AsyncSession, principal):
        try:
            tid = uuid.UUID(transaction_id)
        except ValueError:
            raise ValidationError(f"invalid transaction_id: {transaction_id!r}")
        reversal = await ledger_service.reverse_transaction(
            session,
            principal.tenant.id,
            tid,
            idempotency_key=idempotency_key,
            description=description,
            api_key_id=principal.api_key.id,
            source_ip=principal.source_ip,
        )
        return transaction_dict(
            reversal, await _exponents_for_transaction(session, reversal)
        )

    return await _with_write_session(ctx, run)


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
