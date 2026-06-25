"""Functional checks for the reporting layer (income statement, balance sheet,
balance history) and the shared period parsing.

These reconstruct figures straight from posted journal entries and assert the
accounting equation holds, guarding the modules that ``app/mcp/server.py``
imports for its reporting tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.enums import AccountType, Direction
from app.domain.errors import ValidationError
from app.services import accounts as account_service
from app.services import ledger as ledger_service
from app.services import period, reporting
from app.services.ledger import PostingInput, TransactionInput


async def _account(session, tenant, name, account_type):
    return await account_service.create_account(
        session,
        tenant.id,
        name=name,
        account_type=account_type,
        currency_code="USD",
    )


async def _post(session, tenant, description, debit, credit, amount):
    await ledger_service.post_transaction(
        session,
        tenant.id,
        TransactionInput(
            description=description,
            postings=[
                PostingInput(
                    account_id=debit.id, direction=Direction.DEBIT, amount=Decimal(amount)
                ),
                PostingInput(
                    account_id=credit.id, direction=Direction.CREDIT, amount=Decimal(amount)
                ),
            ],
        ),
    )


@pytest.fixture
async def books(session, tenant):
    """A tiny set of books: invest capital, make a sale, pay rent."""
    cash = await _account(session, tenant, "Cash", AccountType.ASSET)
    capital = await _account(session, tenant, "Capital", AccountType.EQUITY)
    sales = await _account(session, tenant, "Sales", AccountType.REVENUE)
    rent = await _account(session, tenant, "Rent", AccountType.EXPENSE)

    await _post(session, tenant, "owner invests", cash, capital, "1000.00")
    await _post(session, tenant, "cash sale", cash, sales, "300.00")
    await _post(session, tenant, "pay rent", rent, cash, "100.00")
    return {"cash": cash, "capital": capital, "sales": sales, "rent": rent}


async def test_income_statement_nets_revenue_and_expenses(session, tenant, books):
    stmt = await reporting.income_statement(session, tenant.id)
    assert len(stmt.currencies) == 1
    usd = stmt.currencies[0]
    assert usd.currency == "USD"
    assert usd.total_revenue == Decimal("300.00")
    assert usd.total_expenses == Decimal("100.00")
    assert usd.net_income == Decimal("200.00")
    assert {line.account_name for line in usd.revenue} == {"Sales"}
    assert {line.account_name for line in usd.expenses} == {"Rent"}


async def test_balance_sheet_balances(session, tenant, books):
    as_of = period.parse_as_of_inclusive(datetime.now(UTC).date().isoformat())
    sheet = await reporting.balance_sheet(session, tenant.id, as_of=as_of)
    usd = sheet.currencies[0]
    assert usd.total_assets == Decimal("1200.00")  # 1000 + 300 - 100
    assert usd.total_liabilities == Decimal("0")
    assert usd.total_equity == Decimal("1000.00")
    assert usd.retained_earnings == Decimal("200.00")  # net income, not yet closed
    # assets == liabilities + equity + retained earnings
    assert usd.balanced is True


async def test_balance_history_final_point_is_current_balance(session, tenant, books):
    points = await reporting.balance_history(
        session, tenant.id, books["cash"].id, granularity="month"
    )
    assert points, "expected at least one period"
    assert points[-1].closing_balance == Decimal("1200.00")


async def test_balance_history_rejects_bad_granularity(session, tenant, books):
    with pytest.raises(ValidationError):
        await reporting.balance_history(
            session, tenant.id, books["cash"].id, granularity="hour"
        )


def test_period_parsing_conventions():
    # Inclusive start.
    assert period.parse_start("2026-06-01") == datetime(2026, 6, 1, tzinfo=UTC)
    # Date-only end is exclusive next-midnight, so the whole day is included.
    assert period.parse_end_exclusive("2026-06-30") == datetime(2026, 7, 1, tzinfo=UTC)
    # "as of" a date covers through the last microsecond of that day.
    as_of = period.parse_as_of_inclusive("2026-06-30")
    assert as_of == datetime(2026, 6, 30, 23, 59, 59, 999999, tzinfo=UTC)
    # None bounds stay None; bad input raises.
    assert period.parse_start(None) is None
    with pytest.raises(ValidationError):
        period.parse_start("not-a-date")
