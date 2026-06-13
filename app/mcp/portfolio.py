"""Portfolio rollups for MCP financial-analysis tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountType
from app.services import accounts as account_service
from app.services import balances as balance_service


@dataclass(slots=True)
class CurrencyPortfolio:
    currency: str
    assets: Decimal
    liabilities: Decimal
    revenue: Decimal
    expense: Decimal

    @property
    def net_worth(self) -> Decimal:
        return self.assets - self.liabilities

    @property
    def profit_and_loss(self) -> Decimal:
        return self.revenue - self.expense


async def portfolio_summary(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[CurrencyPortfolio]:
    """Roll up balances by currency and account type (mirrors the React dashboard)."""
    accounts = await account_service.list_accounts(session, tenant_id, limit=500)
    rows: dict[str, CurrencyPortfolio] = {}

    for account in accounts:
        view = await balance_service.get_account_balance(session, tenant_id, account.id)
        row = rows.get(
            view.currency,
            CurrencyPortfolio(
                currency=view.currency,
                assets=Decimal(0),
                liabilities=Decimal(0),
                revenue=Decimal(0),
                expense=Decimal(0),
            ),
        )
        account_type = AccountType(account.type)
        if account_type is AccountType.ASSET:
            row.assets += view.balance
        elif account_type is AccountType.LIABILITY:
            row.liabilities += view.balance
        elif account_type is AccountType.REVENUE:
            row.revenue += view.balance
        elif account_type is AccountType.EXPENSE:
            row.expense += view.balance
        rows[view.currency] = row

    return sorted(rows.values(), key=lambda row: row.currency)
