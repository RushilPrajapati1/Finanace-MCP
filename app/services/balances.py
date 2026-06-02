"""Balance reads and ledger-integrity checks."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AccountType, Direction, normal_balance
from app.domain.errors import AccountNotFoundError
from app.domain.money import minor_to_decimal
from app.models import Account, AccountBalance, Currency, Posting


@dataclass(slots=True)
class AccountBalanceView:
    account_id: uuid.UUID
    currency: str
    normal_balance: Direction
    debits: Decimal
    credits: Decimal
    balance: Decimal


@dataclass(slots=True)
class CurrencyTotals:
    currency: str
    debits: Decimal
    credits: Decimal
    difference: Decimal
    balanced: bool


@dataclass(slots=True)
class TrialBalance:
    balanced: bool
    currencies: list[CurrencyTotals]


async def get_account_balance(
    session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> AccountBalanceView:
    account = await session.scalar(
        select(Account).where(
            Account.id == account_id, Account.tenant_id == tenant_id
        )
    )
    if account is None:
        raise AccountNotFoundError(f"account {account_id} not found")

    balance = await session.get(AccountBalance, account_id)
    currency = await session.get(Currency, account.currency_code)
    exponent = currency.exponent if currency else 0

    debits = int(balance.posted_debits) if balance else 0
    credits = int(balance.posted_credits) if balance else 0

    account_type = AccountType(account.type)
    nb = normal_balance(account_type)
    signed = debits - credits if nb is Direction.DEBIT else credits - debits

    return AccountBalanceView(
        account_id=account_id,
        currency=account.currency_code,
        normal_balance=nb,
        debits=minor_to_decimal(debits, exponent),
        credits=minor_to_decimal(credits, exponent),
        balance=minor_to_decimal(signed, exponent),
    )


async def trial_balance(
    session: AsyncSession, tenant_id: uuid.UUID
) -> TrialBalance:
    """Sum debits and credits per currency across the tenant.

    Because every posted transaction balances per currency, a healthy ledger's
    trial balance has ``debits == credits`` for every currency. Any non-zero
    difference signals corruption.
    """
    rows = (
        await session.execute(
            select(
                AccountBalance.currency_code,
                func.coalesce(func.sum(AccountBalance.posted_debits), 0),
                func.coalesce(func.sum(AccountBalance.posted_credits), 0),
            )
            .where(AccountBalance.tenant_id == tenant_id)
            .group_by(AccountBalance.currency_code)
        )
    ).all()

    exponents = await _exponents(session, [r[0] for r in rows])

    lines: list[CurrencyTotals] = []
    all_balanced = True
    for code, debits, credits in rows:
        debits, credits = int(debits), int(credits)
        diff = debits - credits
        balanced = diff == 0
        all_balanced = all_balanced and balanced
        exp = exponents.get(code, 0)
        lines.append(
            CurrencyTotals(
                currency=code,
                debits=minor_to_decimal(debits, exp),
                credits=minor_to_decimal(credits, exp),
                difference=minor_to_decimal(diff, exp),
                balanced=balanced,
            )
        )

    lines.sort(key=lambda line: line.currency)
    return TrialBalance(balanced=all_balanced, currencies=lines)


async def verify_integrity(
    session: AsyncSession, tenant_id: uuid.UUID
) -> dict:
    """Recompute balances straight from postings and diff against the
    materialised ``account_balances`` table.

    This is the audit safety net: if the running totals ever drift from the
    immutable posting history, the discrepancy surfaces here.
    """
    posting_rows = (
        await session.execute(
            select(
                Posting.account_id,
                Posting.direction,
                func.coalesce(func.sum(Posting.amount), 0),
            )
            .where(Posting.tenant_id == tenant_id)
            .group_by(Posting.account_id, Posting.direction)
        )
    ).all()

    computed: dict[uuid.UUID, list[int]] = {}
    for account_id, direction, total in posting_rows:
        entry = computed.setdefault(account_id, [0, 0])
        if direction == Direction.DEBIT.value:
            entry[0] += int(total)
        else:
            entry[1] += int(total)

    stored_rows = (
        await session.execute(
            select(
                AccountBalance.account_id,
                AccountBalance.posted_debits,
                AccountBalance.posted_credits,
            ).where(AccountBalance.tenant_id == tenant_id)
        )
    ).all()
    stored = {
        account_id: (int(d), int(c)) for account_id, d, c in stored_rows
    }

    discrepancies = []
    for account_id in set(stored) | set(computed):
        sd, sc = stored.get(account_id, (0, 0))
        cd, cc = computed.get(account_id, [0, 0])
        if (sd, sc) != (cd, cc):
            discrepancies.append(
                {
                    "account_id": str(account_id),
                    "stored": {"debits": sd, "credits": sc},
                    "computed": {"debits": cd, "credits": cc},
                }
            )

    return {"consistent": not discrepancies, "discrepancies": discrepancies}


async def _exponents(session: AsyncSession, codes) -> dict[str, int]:
    codes = list(set(codes))
    if not codes:
        return {}
    rows = await session.scalars(select(Currency).where(Currency.code.in_(codes)))
    return {c.code: c.exponent for c in rows}
