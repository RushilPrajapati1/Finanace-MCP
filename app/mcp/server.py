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
from decimal import Decimal, InvalidOperation

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
    balance_history_dict,
    balance_sheet_dict,
    batch_results_dict,
    import_results_dict,
    income_statement_dict,
    statement_dict,
    transaction_dict,
    transaction_preview_dict,
    trial_balance_dict,
    validation_result_dict,
)
from app.models import Currency, Transaction
from app.services import accounts as account_service
from app.services import balances as balance_service
from app.services import ledger as ledger_service
from app.services import reporting as reporting_service
from app.services.ledger import PostingInput, TransactionInput
from app.services.period import (
    parse_as_of_inclusive,
    parse_end_exclusive,
    parse_start,
)


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


async def _exponents_for_transactions(
    session: AsyncSession, transactions
) -> dict[str, int]:
    codes: set[str] = set()
    for txn in transactions:
        codes.update(p.currency_code for p in txn.postings)
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


class AccountImportArg(BaseModel):
    """One chart-of-accounts row for :func:`import_accounts`."""

    name: str = Field(description="human-readable account name")
    account_type: str = Field(
        description="one of: asset, liability, equity, revenue, expense"
    )
    currency: str = Field(description="ISO currency code, e.g. 'USD'")
    external_id: str | None = Field(
        default=None, description="optional stable client-side id (unique per tenant)"
    )


class TransactionArg(BaseModel):
    """One balanced transaction for :func:`batch_post_transactions`."""

    description: str = Field(description="what this transaction records")
    postings: list[PostingArg] = Field(
        description="the legs; >=2 and debits must equal credits per currency"
    )
    idempotency_key: str | None = Field(
        default=None, description="optional key so retries are safe"
    )
    external_id: str | None = Field(
        default=None, description="optional stable client-side id"
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
async def list_accounts(
    ctx: Context, limit: int = 50, include_inactive: bool = False
) -> dict:
    """List chart-of-accounts entries for the authenticated tenant.

    Deactivated (archived) accounts are hidden by default; pass
    ``include_inactive=True`` to include them.
    """
    limit = max(1, min(limit, 200))

    async def run(session: AsyncSession, tenant):
        accounts = await account_service.list_accounts(
            session, tenant.id, limit=limit, include_inactive=include_inactive
        )
        return {"accounts": [account_dict(a) for a in accounts]}

    return await _with_session(ctx, run)


@mcp.tool()
async def get_account(
    ctx: Context,
    account_id: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Fetch a single account by ``account_id`` (UUID) or ``external_id``.

    Provide exactly one of the two identifiers. Returns the full account record
    (id, name, type, currency, external_id, is_active, created_at).
    """
    if (account_id is None) == (external_id is None):
        return {
            "error": "validation_error",
            "message": "provide exactly one of account_id or external_id",
        }

    aid: uuid.UUID | None = None
    if account_id is not None:
        try:
            aid = uuid.UUID(account_id)
        except ValueError:
            return {
                "error": "validation_error",
                "message": f"invalid account_id: {account_id!r}",
            }

    async def run(session: AsyncSession, tenant):
        if aid is not None:
            account = await account_service.get_account(session, tenant.id, aid)
        else:
            account = await account_service.get_account_by_external_id(
                session, tenant.id, external_id
            )
        return account_dict(account)

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
async def get_transaction(ctx: Context, transaction_id: str) -> dict:
    """Fetch one transaction by UUID, with its full set of postings."""
    try:
        tid = uuid.UUID(transaction_id)
    except ValueError:
        return {
            "error": "validation_error",
            "message": f"invalid transaction_id: {transaction_id!r}",
        }

    async def run(session: AsyncSession, tenant):
        txn = await ledger_service.get_transaction(session, tenant.id, tid)
        return transaction_dict(
            txn, await _exponents_for_transaction(session, txn)
        )

    return await _with_session(ctx, run)


@mcp.tool()
async def search_transactions(
    ctx: Context,
    account_id: str | None = None,
    external_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_amount: str | None = None,
    max_amount: str | None = None,
    description_query: str | None = None,
    currency: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Search transactions with combinable filters (AND semantics).

    All filters are optional:
      * ``account_id`` / ``external_id`` — only transactions touching that account
        (give one or the other; ``external_id`` is resolved to its account).
      * ``start_date`` / ``end_date`` — ISO dates or timestamps. The range is
        [start, end]; a date-only ``end_date`` includes the whole day.
      * ``min_amount`` / ``max_amount`` — decimal **strings**; matched against a
        posting's amount in its own currency.
      * ``description_query`` — case-insensitive substring of the description.
      * ``currency`` — ISO code.
    Returns the matching transactions (newest first) plus ``total_count`` and the
    ``limit``/``offset`` echoed back for pagination.
    """
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    aid: uuid.UUID | None = None
    if account_id is not None:
        try:
            aid = uuid.UUID(account_id)
        except ValueError:
            return {
                "error": "validation_error",
                "message": f"invalid account_id: {account_id!r}",
            }

    def _amount(value: str | None, field: str):
        if value is None:
            return None, None
        try:
            return Decimal(value), None
        except (InvalidOperation, ValueError):
            return None, {
                "error": "validation_error",
                "message": f"invalid {field}: {value!r} (use a decimal string)",
            }

    min_dec, err = _amount(min_amount, "min_amount")
    if err:
        return err
    max_dec, err = _amount(max_amount, "max_amount")
    if err:
        return err

    async def run(session: AsyncSession, tenant):
        resolved = aid
        if resolved is None and external_id is not None:
            account = await account_service.get_account_by_external_id(
                session, tenant.id, external_id
            )
            resolved = account.id

        result = await ledger_service.search_transactions(
            session,
            tenant.id,
            account_id=resolved,
            start=parse_start(start_date),
            end=parse_end_exclusive(end_date),
            min_amount=min_dec,
            max_amount=max_dec,
            description_query=description_query,
            currency=currency,
            limit=limit,
            offset=offset,
        )
        exponents = await _exponents_for_transactions(session, result.transactions)
        return {
            "transactions": [
                transaction_dict(txn, exponents) for txn in result.transactions
            ],
            "total_count": result.total_count,
            "limit": limit,
            "offset": offset,
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
async def get_income_statement(
    ctx: Context,
    start_date: str,
    end_date: str,
    currency: str | None = None,
) -> dict:
    """Profit & loss (revenue − expenses) over a date range.

    ``start_date`` / ``end_date`` are ISO dates or timestamps; the range is
    [start, end] with a date-only ``end_date`` including the whole day. Results
    are grouped by account and split per currency, with ``total_revenue``,
    ``total_expenses`` and ``net_income`` for each. Pass ``currency`` to restrict
    to one.
    """
    async def run(session: AsyncSession, tenant):
        statement = await reporting_service.income_statement(
            session,
            tenant.id,
            start=parse_start(start_date),
            end=parse_end_exclusive(end_date),
            currency=currency,
        )
        return income_statement_dict(statement)

    return await _with_session(ctx, run)


@mcp.tool()
async def get_balance_sheet(
    ctx: Context,
    as_of_date: str,
    currency: str | None = None,
) -> dict:
    """Assets / liabilities / equity as of a date.

    Cumulative balances of every posting up to and including ``as_of_date``,
    grouped by account and split per currency. ``retained_earnings`` (cumulative
    revenue − expenses not yet closed to equity) is reported so the accounting
    equation holds: ``total_assets == total_liabilities + total_equity +
    retained_earnings`` (surfaced as ``balanced``).
    """
    async def run(session: AsyncSession, tenant):
        sheet = await reporting_service.balance_sheet(
            session,
            tenant.id,
            as_of=parse_as_of_inclusive(as_of_date),
            currency=currency,
        )
        return balance_sheet_dict(sheet)

    return await _with_session(ctx, run)


@mcp.tool()
async def get_balance_history(
    ctx: Context,
    account_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "month",
) -> dict:
    """Time-series of one account's closing balance, for charting trends.

    ``granularity`` is one of ``day``, ``week``, ``month`` (default ``month``).
    Each point is the account's closing balance at the end of that period,
    carried forward across periods with no activity. The final period's
    ``closing_balance`` equals the account's current balance.
    """
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {
            "error": "validation_error",
            "message": f"invalid account_id: {account_id!r}",
        }

    async def run(session: AsyncSession, tenant):
        points = await reporting_service.balance_history(
            session,
            tenant.id,
            aid,
            start=parse_start(start_date),
            end=parse_end_exclusive(end_date),
            granularity=granularity,
        )
        return balance_history_dict(account_id, granularity, points)

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
async def update_account(
    ctx: Context,
    account_id: str,
    name: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Rename an account or change its ``external_id``.

    Only ``name`` and ``external_id`` are mutable — an account's ``type`` and
    ``currency`` are fixed at creation so historical reports stay correct. A new
    ``external_id`` must be unique within the tenant. Returns the updated record.
    """
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {
            "error": "validation_error",
            "message": f"invalid account_id: {account_id!r}",
        }

    async def run(session: AsyncSession, principal):
        account = await account_service.update_account(
            session,
            principal.tenant.id,
            aid,
            name=name,
            external_id=external_id,
        )
        return account_dict(account)

    return await _with_write_session(ctx, run)


@mcp.tool()
async def deactivate_account(ctx: Context, account_id: str) -> dict:
    """Archive (soft-close) an account so it can no longer receive new postings.

    This is never a hard delete: all history is preserved and the account still
    appears in statements and reports. Rejected if the account still holds a
    non-zero balance — zero it out with a transfer first.
    """
    try:
        aid = uuid.UUID(account_id)
    except ValueError:
        return {
            "error": "validation_error",
            "message": f"invalid account_id: {account_id!r}",
        }

    async def run(session: AsyncSession, principal):
        account = await account_service.deactivate_account(
            session, principal.tenant.id, aid
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
async def validate_transaction(
    ctx: Context,
    description: str,
    postings: list[PostingArg],
) -> dict:
    """Dry-run a transaction: check it *would* post, committing nothing.

    Runs the exact validation ``post_transaction`` runs — at least two postings,
    debits equal credits per currency, accounts exist and are active, currencies
    agree, amounts positive. Returns ``{ valid, errors, computed_totals,
    balance_impact }``. Unlike ``post_transaction(dry_run=True)`` this never
    surfaces a hard error for a bad entry: an invalid set returns ``valid=false``
    with the reasons in ``errors``. The ledger is left untouched.
    """
    async def run(session: AsyncSession, tenant):
        try:
            data = TransactionInput(
                postings=_to_posting_inputs(postings),
                description=description,
            )
        except LedgerError as exc:
            return {
                "valid": False,
                "errors": [{"code": exc.code, "message": str(exc)}],
                "computed_totals": [],
            }
        result = await ledger_service.validate_transaction(session, tenant.id, data)
        return validation_result_dict(result)

    return await _with_session(ctx, run)


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


@mcp.tool()
async def import_accounts(
    ctx: Context,
    accounts: list[AccountImportArg],
    skip_existing: bool = True,
) -> dict:
    """Create many accounts in one call (e.g. a whole chart of accounts).

    Per-row best-effort: each row is validated and created independently and the
    response reports ``created`` / ``skipped`` / ``error`` per row plus a
    ``summary``. With ``skip_existing=True`` (default) a row whose ``external_id``
    already exists is skipped (so re-running an import is safe and creates
    nothing new); set it False to have duplicates reported as errors.
    """
    rows = [
        {
            "name": a.name,
            "type": a.account_type,
            "currency": a.currency,
            "external_id": a.external_id,
        }
        for a in accounts
    ]

    async def run(session: AsyncSession, principal):
        results = await account_service.import_accounts(
            session, principal.tenant.id, rows, skip_existing=skip_existing
        )
        return import_results_dict(results)

    return await _with_write_session(ctx, run)


@mcp.tool()
async def batch_post_transactions(
    ctx: Context,
    transactions: list[TransactionArg],
    atomic: bool = True,
) -> dict:
    """Post multiple balanced transactions in one call.

    ``atomic=True`` (default): all-or-nothing — if any transaction is invalid the
    whole batch is rolled back (the bad one is reported as ``error``, the rest as
    ``rolled_back``). ``atomic=False``: best-effort, each isolated so a bad one
    fails alone and the valid ones still post. The response reports a result per
    item plus a ``summary``. Each item may carry its own ``idempotency_key``; if
    omitted, one is derived from its content so accidental retries don't
    double-post.
    """
    async def run(session: AsyncSession, principal):
        items = [
            TransactionInput(
                postings=_to_posting_inputs(t.postings),
                description=t.description,
                idempotency_key=t.idempotency_key
                or _derive_idempotency_key(t.description, t.postings),
                external_id=t.external_id,
                actor="mcp-agent",
            )
            for t in transactions
        ]
        results = await ledger_service.batch_post_transactions(
            session,
            principal.tenant.id,
            items,
            atomic=atomic,
            api_key_id=principal.api_key.id,
            source_ip=principal.source_ip,
        )
        posted = [r.transaction for r in results if r.transaction is not None]
        exponents = await _exponents_for_transactions(session, posted)
        return batch_results_dict(results, exponents)

    return await _with_write_session(ctx, run)


@mcp.tool()
async def get_schema(ctx: Context) -> dict:
    """Describe the ledger's vocabulary so clients can self-configure.

    Returns the valid account types, posting directions, the currencies
    registered on this server (with their decimal exponents), the core posting
    rules, and field conventions — so a client need not hardcode any of them.
    """
    async def run(session: AsyncSession, tenant):
        currencies = (
            await session.scalars(select(Currency).order_by(Currency.code))
        ).all()
        return {
            "account_types": [t.value for t in AccountType],
            "directions": [d.value for d in Direction],
            "currencies": [
                {"code": c.code, "exponent": c.exponent, "name": c.name}
                for c in currencies
            ],
            "posting_rules": [
                "A transaction needs at least two postings.",
                "Within each currency, total debits must equal total credits.",
                "Posting amounts must be strictly positive.",
                "A posting's currency must match its account's currency.",
                "Postings to inactive accounts are rejected.",
                "Posted transactions are immutable; undo with a reversal.",
            ],
            "field_constraints": {
                "amount": "decimal string in major units, e.g. '150.00' (never a float)",
                "date": "ISO-8601 date (YYYY-MM-DD) or full timestamp",
                "external_id": "optional, unique per tenant",
                "idempotency_key": "optional on writes; replays return the original",
            },
        }

    return await _with_session(ctx, run)


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
