"""JSON-friendly serializers for MCP tool responses."""

from __future__ import annotations

from decimal import Decimal

from app.models import Account, Transaction
from app.services.balances import (
    AccountBalanceView,
    CurrencyTotals,
    StatementEntry,
    TrialBalance,
)


def _money(value: Decimal) -> str:
    return format(value, "f")


def account_dict(account: Account) -> dict:
    return {
        "id": str(account.id),
        "name": account.name,
        "type": account.type,
        "currency": account.currency_code,
        "external_id": account.external_id,
        "is_active": account.is_active,
        "created_at": account.created_at.isoformat(),
    }


def balance_dict(view: AccountBalanceView) -> dict:
    return {
        "account_id": str(view.account_id),
        "currency": view.currency,
        "normal_balance": view.normal_balance.value,
        "debits": _money(view.debits),
        "credits": _money(view.credits),
        "balance": _money(view.balance),
    }


def statement_dict(entry: StatementEntry) -> dict:
    return {
        "transaction_id": str(entry.transaction_id),
        "posting_id": str(entry.posting_id),
        "direction": entry.direction.value,
        "amount": _money(entry.amount),
        "balance_after": _money(entry.balance_after),
        "currency": entry.currency,
        "description": entry.description,
        "created_at": entry.created_at.isoformat(),
    }


def trial_balance_dict(trial: TrialBalance) -> dict:
    return {
        "balanced": trial.balanced,
        "currencies": [
            {
                "currency": line.currency,
                "debits": _money(line.debits),
                "credits": _money(line.credits),
                "difference": _money(line.difference),
                "balanced": line.balanced,
            }
            for line in trial.currencies
        ],
    }


def transaction_preview_dict(preview) -> dict:
    """Serialize a :class:`app.services.ledger.TransactionPreview` (dry-run result)."""
    return {
        "dry_run": True,
        "balanced": preview.balanced,
        "description": preview.description,
        "balance_impact": [
            {
                "account_id": str(line.account_id),
                "account": line.account_name,
                "currency": line.currency,
                "change": _money(line.change),
                "balance_before": _money(line.balance_before),
                "balance_after": _money(line.balance_after),
            }
            for line in preview.lines
        ],
    }


def transaction_dict(transaction: Transaction, exponents: dict[str, int]) -> dict:
    from app.domain.money import minor_to_decimal

    return {
        "id": str(transaction.id),
        "description": transaction.description,
        "external_id": transaction.external_id,
        "idempotency_key": transaction.idempotency_key,
        "reversal_of": str(transaction.reversal_of) if transaction.reversal_of else None,
        "created_at": transaction.created_at.isoformat(),
        "postings": [
            {
                "id": str(posting.id),
                "account_id": str(posting.account_id),
                "direction": posting.direction,
                "amount": _money(
                    minor_to_decimal(int(posting.amount), exponents.get(posting.currency_code, 0))
                ),
                "currency": posting.currency_code,
            }
            for posting in transaction.postings
        ],
    }
