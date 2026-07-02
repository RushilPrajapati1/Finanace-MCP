"""Unit tests for the Plaid -> double-entry mapper (pure functions, no network)."""

from __future__ import annotations

from app.integrations.plaid.mapper import (
    amount_to_decimal_str,
    ledger_account_for,
    map_transaction,
)

CHECKING = {
    "account_id": "acc-checking",
    "name": "Plaid Checking",
    "official_name": "Plaid Gold Standard 0% Interest Checking",
    "type": "depository",
    "balances": {"iso_currency_code": "USD"},
}
CREDIT_CARD = {
    "account_id": "acc-credit",
    "name": "Plaid Credit Card",
    "official_name": None,
    "type": "credit",
    "balances": {"iso_currency_code": "USD"},
}


def _accounts():
    return {
        "acc-checking": ledger_account_for(CHECKING),
        "acc-credit": ledger_account_for(CREDIT_CARD),
    }


def _txn(**overrides) -> dict:
    base = {
        "transaction_id": "txn-1",
        "account_id": "acc-checking",
        "amount": 4.33,
        "iso_currency_code": "USD",
        "pending": False,
        "name": "Starbucks",
        "merchant_name": "Starbucks",
        "date": "2026-06-30",
        "payment_channel": "in store",
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
    }
    base.update(overrides)
    return base


def test_linked_account_types():
    accounts = _accounts()
    assert accounts["acc-checking"].type == "asset"
    assert accounts["acc-checking"].external_id == "plaid:account:acc-checking"
    assert accounts["acc-credit"].type == "liability"


def test_unsupported_account_currency_is_none():
    exotic = dict(CHECKING, balances={"iso_currency_code": "XPF"})
    assert ledger_account_for(exotic) is None


def test_outflow_debits_expense_credits_linked_account():
    mapped = map_transaction(_txn(), _accounts())
    assert mapped is not None
    assert mapped.amount == "4.33"
    assert mapped.currency == "USD"
    assert mapped.debit.type == "expense"
    assert mapped.debit.external_id == "plaid:expense:food-and-drink:USD"
    assert mapped.credit.external_id == "plaid:account:acc-checking"
    assert mapped.idempotency_key == "plaid:txn-1"
    assert mapped.external_id == "plaid:txn-1"


def test_inflow_debits_linked_account_credits_income():
    mapped = map_transaction(
        _txn(amount=-1500, personal_finance_category={"primary": "INCOME"}),
        _accounts(),
    )
    assert mapped is not None
    assert mapped.amount == "1500.00"
    assert mapped.debit.external_id == "plaid:account:acc-checking"
    assert mapped.credit.type == "revenue"
    assert mapped.credit.external_id == "plaid:income:income:USD"


def test_credit_card_purchase_credits_the_liability():
    mapped = map_transaction(_txn(account_id="acc-credit"), _accounts())
    assert mapped is not None
    assert mapped.credit.external_id == "plaid:account:acc-credit"
    assert mapped.credit.type == "liability"


def test_pending_zero_and_unsupported_rows_are_skipped():
    accounts = _accounts()
    assert map_transaction(_txn(pending=True), accounts) is None
    assert map_transaction(_txn(amount=0), accounts) is None
    assert map_transaction(_txn(iso_currency_code="XPF"), accounts) is None
    assert map_transaction(_txn(account_id="acc-unknown"), accounts) is None


def test_missing_category_falls_back():
    mapped = map_transaction(_txn(personal_finance_category=None), _accounts())
    assert mapped is not None
    assert mapped.debit.external_id == "plaid:expense:uncategorized:USD"


def test_amounts_are_exact_decimals():
    assert amount_to_decimal_str(4.33, "USD") == "4.33"
    assert amount_to_decimal_str(-12.5, "USD") == "12.50"
    assert amount_to_decimal_str(500, "JPY") == "500"
