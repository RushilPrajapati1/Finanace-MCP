"""The posting engine — the core of the double-entry ledger.

Every transaction is validated against the fundamental invariant before it is
written: **within each currency, the sum of debits must equal the sum of
credits.** A transaction that does not balance is rejected; a balanced one is
written atomically together with the running balance updates it implies.

Multi-currency transactions are supported by requiring each currency to balance
*independently* (the rule used by ledgers such as Beancount). An FX trade, for
example, balances USD against USD and EUR against EUR by routing through an
exchange/clearing account.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.enums import AccountType, Direction, balance_sign, normal_balance
from app.domain.errors import (
    AccountNotFoundError,
    AlreadyReversedError,
    CurrencyMismatchError,
    CurrencyNotFoundError,
    InactiveAccountError,
    LedgerError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
    ValidationError,
)
from app.domain.money import Money, MoneyError, minor_to_decimal
from app.models import Account, AccountBalance, Currency, Posting, Transaction


@dataclass(slots=True)
class PostingInput:
    account_id: uuid.UUID
    direction: Direction
    amount: Decimal
    # Optional: if supplied it must match the account's currency. When omitted
    # the account's own currency is used, which is the common case.
    currency: str | None = None


@dataclass(slots=True)
class TransactionInput:
    postings: list[PostingInput]
    description: str | None = None
    idempotency_key: str | None = None
    external_id: str | None = None
    meta: dict | None = field(default=None)
    # Caller-supplied audit principal within the tenant (e.g. the tenant's own
    # user id). Server-derived audit fields (api_key_id, source_ip) are passed
    # separately to the service, not via this client-facing input.
    actor: str | None = None


async def _load_transaction(
    session: AsyncSession, tenant_id: uuid.UUID, transaction_id: uuid.UUID
) -> Transaction | None:
    return await session.scalar(
        select(Transaction)
        .where(Transaction.id == transaction_id, Transaction.tenant_id == tenant_id)
        .options(selectinload(Transaction.postings))
    )


async def _find_by_idempotency_key(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> Transaction | None:
    return await session.scalar(
        select(Transaction)
        .where(
            Transaction.tenant_id == tenant_id,
            Transaction.idempotency_key == key,
        )
        .options(selectinload(Transaction.postings))
    )


async def _lock_balances(
    session: AsyncSession, account_ids
) -> dict[uuid.UUID, AccountBalance]:
    """Lock the affected balance rows ``FOR UPDATE`` in a deterministic (sorted)
    order so concurrent transactions touching overlapping accounts can never
    deadlock."""
    ids = sorted(account_ids)
    rows = (
        await session.execute(
            select(AccountBalance)
            .where(AccountBalance.account_id.in_(ids))
            .order_by(AccountBalance.account_id)
            .with_for_update()
        )
    ).scalars().all()
    return {b.account_id: b for b in rows}


def _signed_balance(balance: AccountBalance, account_type: AccountType) -> int:
    """The account's signed balance in minor units, per its normal-balance side."""
    debits, credits = int(balance.posted_debits), int(balance.posted_credits)
    if normal_balance(account_type) is Direction.DEBIT:
        return debits - credits
    return credits - debits


async def _write_postings(
    session: AsyncSession,
    *,
    transaction_id: uuid.UUID,
    accounts: dict[uuid.UUID, Account],
    rows: list[dict],
    deltas: dict[uuid.UUID, tuple[int, int]],
) -> None:
    """Persist postings with a per-line running-balance snapshot and apply the
    net per-account deltas to the materialised balances.

    The balance rows are locked first, then ``rows`` is walked *in order* and
    each posting is stamped with the account's signed ``balance_before`` /
    ``balance_after`` as it is threaded. The aggregate ``deltas`` then move the
    stored balance once per account (a single version bump); the final threaded
    value equals the new stored balance by construction.
    """
    balances = await _lock_balances(session, deltas.keys())
    missing = deltas.keys() - balances.keys()
    if missing:  # pragma: no cover - balance row is created with the account
        raise AccountNotFoundError(f"no balance row for accounts {sorted(missing)}")

    running = {
        account_id: _signed_balance(balance, AccountType(accounts[account_id].type))
        for account_id, balance in balances.items()
    }

    postings: list[Posting] = []
    for row in rows:
        account = accounts[row["account_id"]]
        sign = balance_sign(AccountType(account.type), Direction(row["direction"]))
        before = running[account.id]
        after = before + sign * row["amount"]
        running[account.id] = after
        postings.append(
            Posting(
                transaction_id=transaction_id,
                balance_before=before,
                balance_after=after,
                **row,
            )
        )
    session.add_all(postings)
    await session.flush()

    for account_id, (debit_delta, credit_delta) in deltas.items():
        balance = balances[account_id]
        balance.posted_debits = int(balance.posted_debits) + debit_delta
        balance.posted_credits = int(balance.posted_credits) + credit_delta
        balance.version += 1


def _build_postings(
    accounts: dict[uuid.UUID, Account],
    exponents: dict[str, int],
    inputs: list[PostingInput],
) -> tuple[list[dict], dict[uuid.UUID, tuple[int, int]]]:
    """Validate inputs and return posting rows plus per-account balance deltas.

    Raises if the transaction does not balance per currency, references an
    unknown/inactive account, or has a currency that disagrees with its account.
    """
    if len(inputs) < 2:
        raise ValidationError("a transaction needs at least two postings")

    sums: dict[str, dict[Direction, int]] = defaultdict(
        lambda: {Direction.DEBIT: 0, Direction.CREDIT: 0}
    )
    deltas: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
    rows: list[dict] = []

    for line in inputs:
        account = accounts.get(line.account_id)
        if account is None:
            raise AccountNotFoundError(f"account {line.account_id} not found")
        if not account.is_active:
            raise InactiveAccountError(f"account {account.id} is inactive")

        if line.currency is not None and line.currency.upper() != account.currency_code:
            raise CurrencyMismatchError(
                f"posting currency {line.currency.upper()} does not match account "
                f"currency {account.currency_code}"
            )

        exponent = exponents[account.currency_code]
        try:
            money = Money.from_decimal(line.amount, account.currency_code, exponent)
        except MoneyError as exc:
            raise ValidationError(str(exc)) from exc
        if money.minor_units <= 0:
            raise ValidationError("posting amounts must be strictly positive")

        sums[account.currency_code][line.direction] += money.minor_units
        if line.direction is Direction.DEBIT:
            deltas[account.id][0] += money.minor_units
        else:
            deltas[account.id][1] += money.minor_units

        rows.append(
            {
                "account_id": account.id,
                "tenant_id": account.tenant_id,
                "direction": line.direction.value,
                "amount": money.minor_units,
                "currency_code": account.currency_code,
            }
        )

    for currency_code, side in sums.items():
        if side[Direction.DEBIT] != side[Direction.CREDIT]:
            raise UnbalancedTransactionError(
                f"{currency_code}: debits ({side[Direction.DEBIT]}) != credits "
                f"({side[Direction.CREDIT]}) (minor units)"
            )

    return rows, {k: (v[0], v[1]) for k, v in deltas.items()}


async def _post_transaction_staged(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: TransactionInput,
    *,
    api_key_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> tuple[Transaction, bool]:
    """Build and write one transaction **without committing**.

    Returns ``(transaction, replayed)``. When ``replayed`` is true the
    ``idempotency_key`` already existed for this tenant and the returned
    transaction is the original — nothing new was written. Shared by
    :func:`post_transaction` (commit-per-call) and
    :func:`batch_post_transactions` (one commit for the whole batch).
    """
    if data.idempotency_key:
        existing = await _find_by_idempotency_key(
            session, tenant_id, data.idempotency_key
        )
        if existing is not None:
            return existing, True

    account_ids = {line.account_id for line in data.postings}
    accounts = {
        a.id: a
        for a in (
            await session.scalars(
                select(Account).where(
                    Account.tenant_id == tenant_id, Account.id.in_(account_ids)
                )
            )
        ).all()
    }
    exponents = await _exponents_for_accounts(session, accounts.values())

    rows, deltas = _build_postings(accounts, exponents, data.postings)

    transaction = Transaction(
        tenant_id=tenant_id,
        description=data.description,
        idempotency_key=data.idempotency_key,
        external_id=data.external_id,
        meta=data.meta,
        api_key_id=api_key_id,
        actor=data.actor,
        source_ip=source_ip,
    )
    session.add(transaction)
    await session.flush()  # assign transaction.id

    await _write_postings(
        session,
        transaction_id=transaction.id,
        accounts=accounts,
        rows=rows,
        deltas=deltas,
    )
    return transaction, False


async def post_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: TransactionInput,
    *,
    api_key_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> Transaction:
    """Validate and atomically post a balanced transaction.

    Idempotent on ``idempotency_key``: replaying a request with a key already
    seen for this tenant returns the original transaction and posts nothing new.

    ``api_key_id`` and ``source_ip`` are the server-derived audit context (which
    credential, from where); ``data.actor`` is the caller-supplied principal.
    """
    transaction, replayed = await _post_transaction_staged(
        session, tenant_id, data, api_key_id=api_key_id, source_ip=source_ip
    )
    if replayed:
        return transaction

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Lost an idempotency race: another request with the same key won.
        if data.idempotency_key:
            existing = await _find_by_idempotency_key(
                session, tenant_id, data.idempotency_key
            )
            if existing is not None:
                return existing
        raise ValidationError("could not post transaction") from exc

    refreshed = await _load_transaction(session, tenant_id, transaction.id)
    assert refreshed is not None
    return refreshed


@dataclass(slots=True)
class BatchItemResult:
    index: int
    status: str  # 'posted' | 'replayed' | 'error' | 'rolled_back'
    transaction: Transaction | None = None
    error: dict | None = None


async def batch_post_transactions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    items: list[TransactionInput],
    *,
    atomic: bool = True,
    api_key_id: uuid.UUID | None = None,
    source_ip: str | None = None,
) -> list[BatchItemResult]:
    """Post many transactions in one call.

    ``atomic=True`` (default): every item is staged in a single database
    transaction and committed once — if any item is invalid the **whole batch is
    rolled back** (the offending item is reported as ``error``, the rest as
    ``rolled_back``). ``atomic=False``: best-effort per item, each isolated by a
    savepoint, so a bad item fails alone and the good ones still commit.
    """
    results: list[BatchItemResult] = []

    if atomic:
        staged: list[tuple[int, Transaction, bool]] = []
        try:
            for index, data in enumerate(items):
                txn, replayed = await _post_transaction_staged(
                    session, tenant_id, data,
                    api_key_id=api_key_id, source_ip=source_ip,
                )
                staged.append((index, txn, replayed))
            await session.commit()
        except LedgerError as exc:
            await session.rollback()
            failed = len(staged)  # index that raised
            results = [
                BatchItemResult(index=i, status="rolled_back")
                for i in range(len(items))
            ]
            results[failed] = BatchItemResult(
                index=failed,
                status="error",
                error={"code": exc.code, "message": str(exc)},
            )
            return results
        except IntegrityError as exc:
            await session.rollback()
            return [
                BatchItemResult(
                    index=i,
                    status="error",
                    error={
                        "code": "validation_error",
                        "message": f"batch could not be committed: {exc.orig}",
                    },
                )
                for i in range(len(items))
            ]

        for index, txn, replayed in staged:
            refreshed = await _load_transaction(session, tenant_id, txn.id)
            results.append(
                BatchItemResult(
                    index=index,
                    status="replayed" if replayed else "posted",
                    transaction=refreshed,
                )
            )
        return results

    # Best-effort: isolate each item in its own savepoint.
    for index, data in enumerate(items):
        try:
            async with session.begin_nested():
                txn, replayed = await _post_transaction_staged(
                    session, tenant_id, data,
                    api_key_id=api_key_id, source_ip=source_ip,
                )
            results.append(
                BatchItemResult(
                    index=index,
                    status="replayed" if replayed else "posted",
                    transaction=txn,
                )
            )
        except LedgerError as exc:
            results.append(
                BatchItemResult(
                    index=index,
                    status="error",
                    error={"code": exc.code, "message": str(exc)},
                )
            )
    await session.commit()

    # Re-load committed transactions so their postings are available.
    for result in results:
        if result.transaction is not None:
            result.transaction = await _load_transaction(
                session, tenant_id, result.transaction.id
            )
    return results


@dataclass(slots=True)
class PreviewLine:
    account_id: uuid.UUID
    account_name: str
    currency: str
    change: Decimal
    balance_before: Decimal
    balance_after: Decimal


@dataclass(slots=True)
class TransactionPreview:
    balanced: bool
    description: str | None
    lines: list[PreviewLine]


async def preview_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: TransactionInput,
) -> TransactionPreview:
    """Validate a transaction and report its effect *without writing anything*.

    Runs the exact same validation as :func:`post_transaction` (balancing per
    currency, account/currency checks, positive amounts) and projects the
    resulting per-account balances. Nothing is flushed, locked, or committed, so
    this is a safe dry-run an agent can use to confirm before posting. Raises the
    same :class:`LedgerError` subclasses a real post would.
    """
    account_ids = {line.account_id for line in data.postings}
    accounts = {
        a.id: a
        for a in (
            await session.scalars(
                select(Account).where(
                    Account.tenant_id == tenant_id, Account.id.in_(account_ids)
                )
            )
        ).all()
    }
    exponents = await _exponents_for_accounts(session, accounts.values())

    # Reuses the real validation core; raises on an unbalanced/invalid entry.
    _rows, deltas = _build_postings(accounts, exponents, data.postings)

    # Read current balances without locking — a preview must not block writers.
    balances = {
        b.account_id: b
        for b in (
            await session.scalars(
                select(AccountBalance).where(AccountBalance.account_id.in_(deltas.keys()))
            )
        ).all()
    }

    lines: list[PreviewLine] = []
    for account_id, (debit_delta, credit_delta) in deltas.items():
        account = accounts[account_id]
        account_type = AccountType(account.type)
        if normal_balance(account_type) is Direction.DEBIT:
            change_minor = debit_delta - credit_delta
        else:
            change_minor = credit_delta - debit_delta

        balance = balances.get(account_id)
        before_minor = (
            _signed_balance(balance, account_type) if balance is not None else 0
        )
        after_minor = before_minor + change_minor
        exponent = exponents[account.currency_code]
        lines.append(
            PreviewLine(
                account_id=account_id,
                account_name=account.name,
                currency=account.currency_code,
                change=minor_to_decimal(change_minor, exponent),
                balance_before=minor_to_decimal(before_minor, exponent),
                balance_after=minor_to_decimal(after_minor, exponent),
            )
        )

    return TransactionPreview(
        balanced=True, description=data.description, lines=lines
    )


async def reverse_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    transaction_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
    description: str | None = None,
    api_key_id: uuid.UUID | None = None,
    actor: str | None = None,
    source_ip: str | None = None,
) -> Transaction:
    """Post a reversing transaction that negates ``transaction_id``.

    The reversal mirrors every original posting with the opposite direction, so
    the two transactions net to zero. A transaction can be reversed at most once
    (enforced by a unique constraint on ``reverses_transaction_id``).
    """
    original = await _load_transaction(session, tenant_id, transaction_id)
    if original is None:
        raise TransactionNotFoundError(f"transaction {transaction_id} not found")

    existing_reversal = await session.scalar(
        select(Transaction)
        .where(Transaction.reverses_transaction_id == original.id)
        .options(selectinload(Transaction.postings))
    )
    if existing_reversal is not None:
        # Treat a repeat reversal under the same idempotency key as a replay.
        if idempotency_key and existing_reversal.idempotency_key == idempotency_key:
            return existing_reversal
        raise AlreadyReversedError(
            f"transaction {original.id} was already reversed by {existing_reversal.id}"
        )

    if idempotency_key:
        prior = await _find_by_idempotency_key(session, tenant_id, idempotency_key)
        if prior is not None:
            return prior

    # Load the referenced accounts so the reversal's postings can be threaded
    # with their own running-balance snapshot.
    account_ids = {p.account_id for p in original.postings}
    accounts = {
        a.id: a
        for a in (
            await session.scalars(
                select(Account).where(
                    Account.tenant_id == tenant_id, Account.id.in_(account_ids)
                )
            )
        ).all()
    }

    reversal = Transaction(
        tenant_id=tenant_id,
        description=description or f"Reversal of {original.id}",
        idempotency_key=idempotency_key,
        reverses_transaction_id=original.id,
        meta={"reversal_of": str(original.id)},
        api_key_id=api_key_id,
        actor=actor,
        source_ip=source_ip,
    )
    session.add(reversal)
    await session.flush()

    rows: list[dict] = []
    deltas: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
    for original_posting in original.postings:
        flipped = Direction(original_posting.direction).opposite
        amount = int(original_posting.amount)
        rows.append(
            {
                "account_id": original_posting.account_id,
                "tenant_id": tenant_id,
                "direction": flipped.value,
                "amount": amount,
                "currency_code": original_posting.currency_code,
            }
        )
        if flipped is Direction.DEBIT:
            deltas[original_posting.account_id][0] += amount
        else:
            deltas[original_posting.account_id][1] += amount

    await _write_postings(
        session,
        transaction_id=reversal.id,
        accounts=accounts,
        rows=rows,
        deltas={k: (v[0], v[1]) for k, v in deltas.items()},
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        existing_reversal = await session.scalar(
            select(Transaction)
            .where(Transaction.reverses_transaction_id == original.id)
            .options(selectinload(Transaction.postings))
        )
        if existing_reversal is not None:
            raise AlreadyReversedError(
                f"transaction {original.id} was already reversed"
            ) from exc
        raise

    refreshed = await _load_transaction(session, tenant_id, reversal.id)
    assert refreshed is not None
    return refreshed


async def get_transaction(
    session: AsyncSession, tenant_id: uuid.UUID, transaction_id: uuid.UUID
) -> Transaction:
    transaction = await _load_transaction(session, tenant_id, transaction_id)
    if transaction is None:
        raise TransactionNotFoundError(f"transaction {transaction_id} not found")
    return transaction


@dataclass(slots=True)
class TransactionSearchResult:
    transactions: list[Transaction]
    total_count: int


async def search_transactions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    description_query: str | None = None,
    currency: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> TransactionSearchResult:
    """Filtered transaction query with AND semantics, applied in the database.

    All filters are optional and combinable. The posting-level filters
    (``account_id``, ``currency``, ``min_amount``, ``max_amount``) match a
    transaction that has **at least one posting** satisfying them together;
    ``start``/``end`` and ``description_query`` match the transaction header.
    Amount thresholds are compared in each posting's own currency (the minor-unit
    amount is scaled back to a decimal via the currency exponent), so they are
    exact and currency-aware.

    Returns the page of transactions (newest first) plus the total match count
    for pagination.
    """
    has_posting_filter = (
        account_id is not None
        or currency is not None
        or min_amount is not None
        or max_amount is not None
    )
    has_amount_filter = min_amount is not None or max_amount is not None

    conditions = [Transaction.tenant_id == tenant_id]
    if start is not None:
        conditions.append(Transaction.created_at >= start)
    if end is not None:
        conditions.append(Transaction.created_at < end)
    if description_query is not None:
        conditions.append(Transaction.description.ilike(f"%{description_query}%"))
    if account_id is not None:
        conditions.append(Posting.account_id == account_id)
    if currency is not None:
        conditions.append(Posting.currency_code == currency.upper())
    if has_amount_filter:
        # Posting.amount is in minor units; scale the decimal threshold up by the
        # currency's exponent (all numeric, no float) to compare like-for-like.
        scale = func.power(cast(10, Numeric), cast(Currency.exponent, Numeric))
        if min_amount is not None:
            conditions.append(Posting.amount >= cast(min_amount, Numeric) * scale)
        if max_amount is not None:
            conditions.append(Posting.amount <= cast(max_amount, Numeric) * scale)

    def _scoped(stmt):
        if has_posting_filter:
            stmt = stmt.join(Posting, Posting.transaction_id == Transaction.id)
            if has_amount_filter:
                stmt = stmt.join(Currency, Currency.code == Posting.currency_code)
        return stmt.where(*conditions)

    total = await session.scalar(
        _scoped(select(func.count(func.distinct(Transaction.id))))
    )

    # Select id + created_at (DISTINCT requires the ORDER BY key in the columns).
    id_rows = (
        await session.execute(
            _scoped(select(Transaction.id, Transaction.created_at).distinct())
            .order_by(Transaction.created_at.desc(), Transaction.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    ids = [row[0] for row in id_rows]

    if not ids:
        return TransactionSearchResult(transactions=[], total_count=int(total or 0))

    loaded = {
        txn.id: txn
        for txn in (
            await session.scalars(
                select(Transaction)
                .where(Transaction.id.in_(ids))
                .options(selectinload(Transaction.postings))
            )
        ).all()
    }
    ordered = [loaded[i] for i in ids if i in loaded]
    return TransactionSearchResult(transactions=ordered, total_count=int(total or 0))


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[dict]
    computed_totals: list[dict]
    preview: TransactionPreview | None


async def validate_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: TransactionInput,
) -> ValidationResult:
    """Dry-run a transaction: report whether it *would* post, without writing.

    Runs the same validation as :func:`post_transaction` (>=2 postings, balanced
    per currency, accounts exist + active, currency agreement, positive amounts).
    Unlike a real post it never raises on a bad entry — it captures the failure
    in ``errors`` so a caller can inspect every problem structurally. Writes
    nothing.
    """
    try:
        preview = await preview_transaction(session, tenant_id, data)
    except LedgerError as exc:
        return ValidationResult(
            valid=False,
            errors=[{"code": exc.code, "message": str(exc)}],
            computed_totals=[],
            preview=None,
        )

    # Recompute per-currency debit/credit totals for the caller's confirmation.
    account_ids = {line.account_id for line in data.postings}
    accounts = {
        a.id: a
        for a in (
            await session.scalars(
                select(Account).where(
                    Account.tenant_id == tenant_id, Account.id.in_(account_ids)
                )
            )
        ).all()
    }
    exponents = await _exponents_for_accounts(session, accounts.values())
    totals: dict[str, dict[Direction, int]] = defaultdict(
        lambda: {Direction.DEBIT: 0, Direction.CREDIT: 0}
    )
    for line in data.postings:
        account = accounts[line.account_id]
        money = Money.from_decimal(
            line.amount, account.currency_code, exponents[account.currency_code]
        )
        totals[account.currency_code][line.direction] += money.minor_units

    computed_totals = [
        {
            "currency": code,
            "debits": format(
                minor_to_decimal(sides[Direction.DEBIT], exponents[code]), "f"
            ),
            "credits": format(
                minor_to_decimal(sides[Direction.CREDIT], exponents[code]), "f"
            ),
        }
        for code, sides in sorted(totals.items())
    ]
    return ValidationResult(
        valid=True, errors=[], computed_totals=computed_totals, preview=preview
    )


async def _exponents_for_accounts(
    session: AsyncSession, accounts
) -> dict[str, int]:
    codes = {a.currency_code for a in accounts}
    if not codes:
        return {}
    rows = await session.scalars(select(Currency).where(Currency.code.in_(codes)))
    exponents = {c.code: c.exponent for c in rows}
    missing = codes - exponents.keys()
    if missing:
        raise CurrencyNotFoundError(f"unknown currencies: {sorted(missing)}")
    return exponents
