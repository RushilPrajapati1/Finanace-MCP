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
from decimal import Decimal

from sqlalchemy import select
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
    TransactionNotFoundError,
    UnbalancedTransactionError,
    ValidationError,
)
from app.domain.money import Money, MoneyError
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


async def post_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    data: TransactionInput,
) -> Transaction:
    """Validate and atomically post a balanced transaction.

    Idempotent on ``idempotency_key``: replaying a request with a key already
    seen for this tenant returns the original transaction and posts nothing new.
    """
    if data.idempotency_key:
        existing = await _find_by_idempotency_key(
            session, tenant_id, data.idempotency_key
        )
        if existing is not None:
            return existing

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


async def reverse_transaction(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    transaction_id: uuid.UUID,
    *,
    idempotency_key: str | None = None,
    description: str | None = None,
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
