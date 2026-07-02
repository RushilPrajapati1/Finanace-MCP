"""Plaid transaction -> balanced FinLedger double entry (pure functions).

Plaid reports each transaction from the *linked account's* perspective as a
single signed row: a **positive** amount is money leaving the account (a
purchase, a transfer out), a **negative** amount is money coming in (a
deposit, a refund). FinLedger is strict double-entry, so every row becomes a
two-leg transaction between the linked account and a category counterparty:

    * amount > 0 (outflow) -> debit  Expenses:<category>,  credit <linked account>
    * amount < 0 (inflow)  -> debit  <linked account>,     credit Income:<category>

The linked account's ledger type follows Plaid's account type (``depository``
and ``investment`` are assets; ``credit`` and ``loan`` are liabilities), so the
same debit/credit rule is correct for a credit card too: a purchase credits
(grows) the liability, a repayment debits (shrinks) it.

Idempotency: Plaid transaction ids are stable, so ``plaid:<transaction_id>``
serves as both the FinLedger ``idempotency_key`` and ``external_id`` — re-runs
and overlapping sync windows post nothing new. Pending transactions are
skipped; they are re-delivered by ``/transactions/sync`` with a new id once
they settle.

Amounts arrive as JSON numbers; they are converted via ``Decimal(str(...))``
and quantized to the currency's ISO exponent so no float ever reaches the
ledger.
"""

from __future__ import annotations

from decimal import Decimal

from app.integrations.opencollective import (
    CURRENCY_EXPONENTS,
    AccountSpec,
    MappedTxn,
)

# Plaid account.type -> FinLedger account type. Money owed to the user is an
# asset; money the user owes is a liability.
_ACCOUNT_TYPES = {
    "depository": "asset",
    "investment": "asset",
    "brokerage": "asset",
    "credit": "liability",
    "loan": "liability",
}

_FALLBACK_CATEGORY = "uncategorized"


def amount_to_decimal_str(amount: float | int | str, currency: str) -> str:
    """Signed Plaid amount -> absolute major-unit decimal string, exactly.

    ``Decimal(str(x))`` preserves the JSON literal (e.g. ``4.33`` -> "4.33");
    quantizing to the ISO exponent normalises trailing digits ("12.5" -> "12.50").
    """
    exponent = CURRENCY_EXPONENTS[currency]
    magnitude = abs(Decimal(str(amount))).quantize(Decimal(1).scaleb(-exponent))
    return format(magnitude, "f")


def ledger_account_for(plaid_account: dict) -> AccountSpec | None:
    """The chart-of-accounts entry for one linked Plaid account.

    Returns ``None`` when the account's currency is not one FinLedger seeds
    (its transactions are skipped rather than posted with a guessed scale).
    """
    currency = (plaid_account.get("balances") or {}).get("iso_currency_code")
    if currency not in CURRENCY_EXPONENTS:
        return None
    account_id = plaid_account["account_id"]
    name = plaid_account.get("official_name") or plaid_account.get("name") or account_id
    return AccountSpec(
        external_id=f"plaid:account:{account_id}",
        name=f"{name} (Plaid)",
        type=_ACCOUNT_TYPES.get(plaid_account.get("type"), "asset"),
        currency=currency,
    )


def _category_slug(txn: dict) -> str:
    """A stable, readable slug from Plaid's personal finance category."""
    category = (txn.get("personal_finance_category") or {}).get("primary")
    if not category:
        return _FALLBACK_CATEGORY
    return category.lower().replace("_", "-")


def _counterparty(slug: str, currency: str, *, inflow: bool) -> AccountSpec:
    if inflow:
        role, type_ = "income", "revenue"
    else:
        role, type_ = "expense", "expense"
    title = slug.replace("-", " ").title()
    return AccountSpec(
        external_id=f"plaid:{role}:{slug}:{currency}",
        name=f"{title} ({role.title()}, {currency})",
        type=type_,
        currency=currency,
    )


def map_transaction(
    txn: dict, ledger_accounts: dict[str, AccountSpec]
) -> MappedTxn | None:
    """Map one Plaid transaction to a balanced :class:`MappedTxn`.

    ``ledger_accounts`` maps Plaid ``account_id`` -> the linked account's
    :class:`AccountSpec` (from :func:`ledger_account_for`). Returns ``None``
    (skipped) for pending rows, zero amounts, unsupported currencies, or
    transactions on accounts we could not map.
    """
    if txn.get("pending"):
        return None

    currency = txn.get("iso_currency_code")
    if currency not in CURRENCY_EXPONENTS:
        return None

    linked = ledger_accounts.get(txn.get("account_id"))
    if linked is None or linked.currency != currency:
        return None

    amount = Decimal(str(txn.get("amount") or 0))
    if amount == 0:
        return None

    slug = _category_slug(txn)
    outflow = amount > 0
    counterparty = _counterparty(slug, currency, inflow=not outflow)
    if outflow:
        debit, credit = counterparty, linked
    else:
        debit, credit = linked, counterparty

    transaction_id = txn["transaction_id"]
    description = (
        txn.get("merchant_name") or txn.get("name") or f"Plaid {transaction_id}"
    )

    return MappedTxn(
        idempotency_key=f"plaid:{transaction_id}",
        external_id=f"plaid:{transaction_id}",
        description=description,
        currency=currency,
        amount=amount_to_decimal_str(amount, currency),
        debit=debit,
        credit=credit,
        metadata={
            "source": "plaid",
            "transaction_id": transaction_id,
            "plaid_account_id": txn.get("account_id"),
            "category": slug,
            "date": txn.get("date"),
            "payment_channel": txn.get("payment_channel"),
        },
    )
