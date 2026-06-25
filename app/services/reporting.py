"""Financial statements derived straight from the immutable ``postings`` table.

Every figure here is recomputed from journal postings, never from a cached
total, so the income statement, balance sheet and balance history can never
disagree with the trial balance. Money stays in integer minor units through all
arithmetic and is converted to :class:`~decimal.Decimal` only at the edge.

Sign convention (see :func:`app.domain.enums.balance_sign`): each account's
reported amount is the movement in its *normal* direction, so normal activity is
positive — assets/expenses by debits, liabilities/equity/revenue by credits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountType, Direction, balance_sign
from app.domain.errors import ValidationError
from app.domain.money import minor_to_decimal
from app.models import Account, Currency, Posting
from app.services.accounts import get_account

_END_OF_DAY = time(23, 59, 59, 999999)

_INCOME_TYPES = (AccountType.REVENUE.value, AccountType.EXPENSE.value)
_BALANCE_TYPES = (
    AccountType.ASSET.value,
    AccountType.LIABILITY.value,
    AccountType.EQUITY.value,
)


# --------------------------------------------------------------------------- #
# Result shapes (consumed by app.mcp.serializers)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ReportLine:
    account_id: uuid.UUID
    account_name: str
    account_type: str
    amount: Decimal


@dataclass(slots=True)
class IncomeStatementCurrency:
    currency: str
    revenue: list[ReportLine] = field(default_factory=list)
    expenses: list[ReportLine] = field(default_factory=list)
    total_revenue: Decimal = Decimal(0)
    total_expenses: Decimal = Decimal(0)
    net_income: Decimal = Decimal(0)


@dataclass(slots=True)
class IncomeStatement:
    start: datetime | None
    end: datetime | None
    currencies: list[IncomeStatementCurrency]


@dataclass(slots=True)
class BalanceSheetCurrency:
    currency: str
    assets: list[ReportLine] = field(default_factory=list)
    liabilities: list[ReportLine] = field(default_factory=list)
    equity: list[ReportLine] = field(default_factory=list)
    total_assets: Decimal = Decimal(0)
    total_liabilities: Decimal = Decimal(0)
    total_equity: Decimal = Decimal(0)
    retained_earnings: Decimal = Decimal(0)
    balanced: bool = True


@dataclass(slots=True)
class BalanceSheet:
    as_of: datetime
    currencies: list[BalanceSheetCurrency]


@dataclass(slots=True)
class BalancePoint:
    period: str
    closing_balance: Decimal


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _AccountActivity:
    account_id: uuid.UUID
    name: str
    type: str
    currency: str
    debits: int
    credits: int

    def signed_minor(self) -> int:
        """Net movement in the account's normal direction, in minor units."""
        t = AccountType(self.type)
        return self.debits * balance_sign(t, Direction.DEBIT) + self.credits * balance_sign(
            t, Direction.CREDIT
        )


async def _exponents(session: AsyncSession, codes: set[str]) -> dict[str, int]:
    if not codes:
        return {}
    rows = await session.scalars(select(Currency).where(Currency.code.in_(codes)))
    return {c.code: c.exponent for c in rows}


async def _account_activity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    types: tuple[str, ...],
    currency: str | None,
    start: datetime | None = None,
    end_exclusive: datetime | None = None,
    as_of_inclusive: datetime | None = None,
) -> list[_AccountActivity]:
    """Sum debits and credits per account over the given window.

    The window is half-open ``[start, end_exclusive)`` for income reporting, or
    ``created_at <= as_of_inclusive`` for a point-in-time balance sheet. Filters
    are applied in the database against the immutable postings.
    """
    conditions = [Posting.tenant_id == tenant_id, Account.type.in_(types)]
    if currency is not None:
        conditions.append(Account.currency_code == currency.upper())
    if start is not None:
        conditions.append(Posting.created_at >= start)
    if end_exclusive is not None:
        conditions.append(Posting.created_at < end_exclusive)
    if as_of_inclusive is not None:
        conditions.append(Posting.created_at <= as_of_inclusive)

    rows = (
        await session.execute(
            select(
                Account.id,
                Account.name,
                Account.type,
                Account.currency_code,
                Posting.direction,
                func.coalesce(func.sum(Posting.amount), 0),
            )
            .join(Account, Account.id == Posting.account_id)
            .where(*conditions)
            .group_by(
                Account.id,
                Account.name,
                Account.type,
                Account.currency_code,
                Posting.direction,
            )
        )
    ).all()

    acc: dict[uuid.UUID, _AccountActivity] = {}
    for account_id, name, type_, code, direction, total in rows:
        entry = acc.get(account_id)
        if entry is None:
            entry = _AccountActivity(account_id, name, type_, code, 0, 0)
            acc[account_id] = entry
        if direction == Direction.DEBIT.value:
            entry.debits += int(total)
        else:
            entry.credits += int(total)
    return list(acc.values())


def _line(activity: _AccountActivity, exponent: int) -> ReportLine:
    return ReportLine(
        account_id=activity.account_id,
        account_name=activity.name,
        account_type=activity.type,
        amount=minor_to_decimal(activity.signed_minor(), exponent),
    )


# --------------------------------------------------------------------------- #
# Income statement
# --------------------------------------------------------------------------- #
async def income_statement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    currency: str | None = None,
) -> IncomeStatement:
    """Revenue − expenses over ``[start, end)``, grouped by account per currency."""
    activities = await _account_activity(
        session,
        tenant_id,
        types=_INCOME_TYPES,
        currency=currency,
        start=start,
        end_exclusive=end,
    )
    exponents = await _exponents(session, {a.currency for a in activities})

    by_currency: dict[str, IncomeStatementCurrency] = {}
    for a in activities:
        bucket = by_currency.setdefault(a.currency, IncomeStatementCurrency(a.currency))
        line = _line(a, exponents.get(a.currency, 0))
        if a.type == AccountType.REVENUE.value:
            bucket.revenue.append(line)
            bucket.total_revenue += line.amount
        else:
            bucket.expenses.append(line)
            bucket.total_expenses += line.amount

    for bucket in by_currency.values():
        bucket.revenue.sort(key=lambda line: line.account_name)
        bucket.expenses.sort(key=lambda line: line.account_name)
        bucket.net_income = bucket.total_revenue - bucket.total_expenses

    currencies = [by_currency[c] for c in sorted(by_currency)]
    return IncomeStatement(start=start, end=end, currencies=currencies)


# --------------------------------------------------------------------------- #
# Balance sheet
# --------------------------------------------------------------------------- #
async def balance_sheet(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    as_of: datetime,
    currency: str | None = None,
) -> BalanceSheet:
    """Assets / liabilities / equity cumulative through ``as_of`` (inclusive).

    ``retained_earnings`` carries revenue − expenses not yet closed to equity so
    the accounting equation holds; ``balanced`` reports whether
    ``assets == liabilities + equity + retained_earnings`` exactly (compared in
    integer minor units, so it is never a floating-point near-miss).
    """
    balance_rows = await _account_activity(
        session,
        tenant_id,
        types=_BALANCE_TYPES,
        currency=currency,
        as_of_inclusive=as_of,
    )
    income_rows = await _account_activity(
        session,
        tenant_id,
        types=_INCOME_TYPES,
        currency=currency,
        as_of_inclusive=as_of,
    )
    exponents = await _exponents(
        session, {a.currency for a in balance_rows} | {a.currency for a in income_rows}
    )

    # Retained earnings per currency = cumulative net income (revenue − expense).
    retained_minor: dict[str, int] = {}
    for a in income_rows:
        signed = a.signed_minor()
        retained_minor[a.currency] = retained_minor.get(a.currency, 0) + (
            signed if a.type == AccountType.REVENUE.value else -signed
        )

    # Track signed minor-unit totals per currency to test the equation exactly.
    totals_minor: dict[str, dict[str, int]] = {}

    by_currency: dict[str, BalanceSheetCurrency] = {}
    currency_codes = set(retained_minor) | {a.currency for a in balance_rows}
    for code in currency_codes:
        by_currency[code] = BalanceSheetCurrency(code)
        totals_minor[code] = {"assets": 0, "liabilities": 0, "equity": 0}

    for a in balance_rows:
        bucket = by_currency[a.currency]
        exponent = exponents.get(a.currency, 0)
        line = _line(a, exponent)
        signed = a.signed_minor()
        if a.type == AccountType.ASSET.value:
            bucket.assets.append(line)
            bucket.total_assets += line.amount
            totals_minor[a.currency]["assets"] += signed
        elif a.type == AccountType.LIABILITY.value:
            bucket.liabilities.append(line)
            bucket.total_liabilities += line.amount
            totals_minor[a.currency]["liabilities"] += signed
        else:
            bucket.equity.append(line)
            bucket.total_equity += line.amount
            totals_minor[a.currency]["equity"] += signed

    for code, bucket in by_currency.items():
        exponent = exponents.get(code, 0)
        retained = retained_minor.get(code, 0)
        bucket.retained_earnings = minor_to_decimal(retained, exponent)
        totals = totals_minor[code]
        bucket.balanced = totals["assets"] == (
            totals["liabilities"] + totals["equity"] + retained
        )
        bucket.assets.sort(key=lambda line: line.account_name)
        bucket.liabilities.sort(key=lambda line: line.account_name)
        bucket.equity.sort(key=lambda line: line.account_name)

    currencies = [by_currency[c] for c in sorted(by_currency)]
    return BalanceSheet(as_of=as_of, currencies=currencies)


# --------------------------------------------------------------------------- #
# Balance history
# --------------------------------------------------------------------------- #
def _iter_periods(first: date, last: date, granularity: str):
    """Yield ``(label, end_instant)`` for each period covering ``[first, last]``.

    ``end_instant`` is the last microsecond of the period (UTC), the cutoff at
    which the closing balance is taken.
    """
    if granularity == "day":
        cur = first
        while cur <= last:
            yield cur.isoformat(), datetime.combine(cur, _END_OF_DAY, tzinfo=UTC)
            cur += timedelta(days=1)
    elif granularity == "week":
        cur = first - timedelta(days=first.weekday())  # back up to Monday
        while cur <= last:
            iso = cur.isocalendar()
            sunday = cur + timedelta(days=6)
            yield (
                f"{iso.year:04d}-W{iso.week:02d}",
                datetime.combine(sunday, _END_OF_DAY, tzinfo=UTC),
            )
            cur += timedelta(days=7)
    elif granularity == "month":
        cur = first.replace(day=1)
        while cur <= last:
            nxt = (
                cur.replace(year=cur.year + 1, month=1)
                if cur.month == 12
                else cur.replace(month=cur.month + 1)
            )
            last_day = nxt - timedelta(days=1)
            yield (
                f"{cur.year:04d}-{cur.month:02d}",
                datetime.combine(last_day, _END_OF_DAY, tzinfo=UTC),
            )
            cur = nxt
    else:
        raise ValidationError(
            f"invalid granularity {granularity!r}: use 'day', 'week' or 'month'"
        )


async def balance_history(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    granularity: str = "month",
) -> list[BalancePoint]:
    """Closing balance of one account at the end of each period in the range.

    The closing balance is cumulative from inception (every posting up to the
    period's end), so it carries forward across periods with no activity and the
    final point equals the account's current balance. ``start``/``end`` only
    bound which periods are emitted.
    """
    if granularity not in ("day", "week", "month"):
        raise ValidationError(
            f"invalid granularity {granularity!r}: use 'day', 'week' or 'month'"
        )

    account = await get_account(session, tenant_id, account_id)
    account_type = AccountType(account.type)
    exponent = (await _exponents(session, {account.currency_code})).get(
        account.currency_code, 0
    )

    rows = (
        await session.execute(
            select(Posting.created_at, Posting.direction, Posting.amount)
            .where(Posting.account_id == account_id, Posting.tenant_id == tenant_id)
            .order_by(Posting.created_at, Posting.id)
        )
    ).all()

    events: list[tuple[datetime, int]] = [
        (
            created_at,
            int(amount) * balance_sign(account_type, Direction(direction)),
        )
        for created_at, direction, amount in rows
    ]

    now = datetime.now(UTC)
    if start is not None:
        first = start.date()
    elif events:
        first = events[0][0].astimezone(UTC).date()
    else:
        first = now.date()

    if end is not None:
        # ``end`` is the exclusive upper bound; the last included day is the one
        # just before it.
        last = (end - timedelta(microseconds=1)).date()
    elif events:
        last = max(events[-1][0].astimezone(UTC).date(), now.date())
    else:
        last = first

    points: list[BalancePoint] = []
    idx = 0
    running = 0
    for label, end_instant in _iter_periods(first, last, granularity):
        while idx < len(events) and events[idx][0] <= end_instant:
            running += events[idx][1]
            idx += 1
        points.append(
            BalancePoint(period=label, closing_balance=minor_to_decimal(running, exponent))
        )
    return points
