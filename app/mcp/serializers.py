"""JSON-friendly serializers for MCP tool responses."""

from __future__ import annotations

from decimal import Decimal

from app.models import Account, Transaction
from app.services.balances import (
    AccountBalanceView,
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


def validation_result_dict(result) -> dict:
    """Serialize a :class:`app.services.ledger.ValidationResult` (dry-run check)."""
    out = {
        "valid": result.valid,
        "errors": result.errors,
        "computed_totals": result.computed_totals,
    }
    if result.preview is not None:
        out["balance_impact"] = transaction_preview_dict(result.preview)["balance_impact"]
    return out


def _report_line(line) -> dict:
    return {
        "account_id": str(line.account_id),
        "account": line.account_name,
        "type": line.account_type,
        "amount": _money(line.amount),
    }


def income_statement_dict(statement) -> dict:
    return {
        "start_date": statement.start.isoformat() if statement.start else None,
        "end_date": statement.end.isoformat() if statement.end else None,
        "currencies": [
            {
                "currency": c.currency,
                "revenue": [_report_line(line) for line in c.revenue],
                "expenses": [_report_line(line) for line in c.expenses],
                "total_revenue": _money(c.total_revenue),
                "total_expenses": _money(c.total_expenses),
                "net_income": _money(c.net_income),
            }
            for c in statement.currencies
        ],
    }


def balance_sheet_dict(sheet) -> dict:
    return {
        "as_of_date": sheet.as_of.isoformat(),
        "currencies": [
            {
                "currency": c.currency,
                "assets": [_report_line(line) for line in c.assets],
                "liabilities": [_report_line(line) for line in c.liabilities],
                "equity": [_report_line(line) for line in c.equity],
                "total_assets": _money(c.total_assets),
                "total_liabilities": _money(c.total_liabilities),
                "total_equity": _money(c.total_equity),
                "retained_earnings": _money(c.retained_earnings),
                "balanced": c.balanced,
            }
            for c in sheet.currencies
        ],
    }


def balance_history_dict(account_id: str, granularity: str, points) -> dict:
    return {
        "account_id": account_id,
        "granularity": granularity,
        "points": [
            {"period": p.period, "closing_balance": _money(p.closing_balance)}
            for p in points
        ],
    }


def import_results_dict(results) -> dict:
    summary: dict[str, int] = {"created": 0, "skipped": 0, "error": 0}
    rows = []
    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
        row = {
            "index": r.index,
            "external_id": r.external_id,
            "status": r.status,
        }
        if r.account is not None:
            row["account_id"] = str(r.account.id)
        if r.error is not None:
            row["error"] = r.error
        rows.append(row)
    return {"results": rows, "summary": summary}


def batch_results_dict(results, exponents: dict[str, int]) -> dict:
    summary: dict[str, int] = {}
    rows = []
    for r in results:
        summary[r.status] = summary.get(r.status, 0) + 1
        row = {"index": r.index, "status": r.status}
        if r.transaction is not None:
            row["transaction"] = transaction_dict(r.transaction, exponents)
        if r.error is not None:
            row["error"] = r.error
        rows.append(row)
    return {"results": rows, "summary": summary}


def transaction_dict(transaction: Transaction, exponents: dict[str, int]) -> dict:
    from app.domain.money import minor_to_decimal

    return {
        "id": str(transaction.id),
        "description": transaction.description,
        "external_id": transaction.external_id,
        "idempotency_key": transaction.idempotency_key,
        "reversal_of": str(transaction.reverses_transaction_id) if transaction.reverses_transaction_id else None,
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
