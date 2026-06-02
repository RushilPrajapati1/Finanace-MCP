"""Pydantic request/response models for the HTTP API.

Monetary fields are serialised as **strings** (e.g. ``"100.00"``) rather than
JSON numbers so that no precision is lost in transit. Clients should likewise
send amounts as strings.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, PlainSerializer

from app.domain.enums import AccountType, Direction
from app.domain.money import minor_to_decimal
from app.services.balances import AccountBalanceView, CurrencyTotals, TrialBalance

# Serialise Decimal as a plain (non-scientific) string in JSON responses.
MoneyStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json"),
]


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
class AccountCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    type: AccountType
    currency: str = Field(..., min_length=2, max_length=8)
    external_id: str | None = Field(default=None, max_length=255)
    metadata: dict | None = None


class AccountOut(BaseModel):
    id: UUID
    name: str
    type: AccountType
    currency: str
    external_id: str | None
    is_active: bool
    metadata: dict | None
    created_at: datetime

    @classmethod
    def from_model(cls, account) -> AccountOut:
        return cls(
            id=account.id,
            name=account.name,
            type=AccountType(account.type),
            currency=account.currency_code,
            external_id=account.external_id,
            is_active=account.is_active,
            metadata=account.meta,
            created_at=account.created_at,
        )


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #
class PostingIn(BaseModel):
    account_id: UUID
    direction: Direction
    amount: Decimal = Field(..., gt=0, description="Amount as a decimal string")
    currency: str | None = Field(
        default=None,
        description="Optional; if set, must match the account's currency.",
    )


class TransactionCreate(BaseModel):
    postings: list[PostingIn] = Field(..., min_length=2)
    description: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)
    external_id: str | None = Field(default=None, max_length=255)
    metadata: dict | None = None


class ReversalCreate(BaseModel):
    description: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=255)


class PostingOut(BaseModel):
    id: UUID
    account_id: UUID
    direction: Direction
    amount: MoneyStr
    currency: str


class TransactionOut(BaseModel):
    id: UUID
    description: str | None
    idempotency_key: str | None
    external_id: str | None
    reverses_transaction_id: UUID | None
    metadata: dict | None
    postings: list[PostingOut]
    posted_at: datetime
    created_at: datetime

    @classmethod
    def from_model(cls, transaction, exponents: dict[str, int]) -> TransactionOut:
        return cls(
            id=transaction.id,
            description=transaction.description,
            idempotency_key=transaction.idempotency_key,
            external_id=transaction.external_id,
            reverses_transaction_id=transaction.reverses_transaction_id,
            metadata=transaction.meta,
            posted_at=transaction.posted_at,
            created_at=transaction.created_at,
            postings=[
                PostingOut(
                    id=p.id,
                    account_id=p.account_id,
                    direction=Direction(p.direction),
                    amount=minor_to_decimal(
                        int(p.amount), exponents.get(p.currency_code, 0)
                    ),
                    currency=p.currency_code,
                )
                for p in transaction.postings
            ],
        )


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #
class BalanceOut(BaseModel):
    account_id: UUID
    currency: str
    normal_balance: Direction
    debits: MoneyStr
    credits: MoneyStr
    balance: MoneyStr

    @classmethod
    def from_view(cls, view: AccountBalanceView) -> BalanceOut:
        return cls(
            account_id=view.account_id,
            currency=view.currency,
            normal_balance=view.normal_balance,
            debits=view.debits,
            credits=view.credits,
            balance=view.balance,
        )


class CurrencyTotalsOut(BaseModel):
    currency: str
    debits: MoneyStr
    credits: MoneyStr
    difference: MoneyStr
    balanced: bool

    @classmethod
    def from_model(cls, totals: CurrencyTotals) -> CurrencyTotalsOut:
        return cls(
            currency=totals.currency,
            debits=totals.debits,
            credits=totals.credits,
            difference=totals.difference,
            balanced=totals.balanced,
        )


class TrialBalanceOut(BaseModel):
    balanced: bool
    currencies: list[CurrencyTotalsOut]

    @classmethod
    def from_model(cls, tb: TrialBalance) -> TrialBalanceOut:
        return cls(
            balanced=tb.balanced,
            currencies=[CurrencyTotalsOut.from_model(c) for c in tb.currencies],
        )


class IntegrityOut(BaseModel):
    consistent: bool
    discrepancies: list[dict]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthOut(BaseModel):
    status: str
    service: str
    environment: str
