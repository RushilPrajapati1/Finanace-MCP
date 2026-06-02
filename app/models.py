"""SQLAlchemy ORM models — the ledger schema.

Schema shape and the invariants each table guarantees:

* ``tenants``           one row per fintech company (multi-tenancy boundary).
* ``api_keys``          hashed credentials, each scoped to one tenant.
* ``currencies``        registry of currency codes and their decimal exponent.
* ``accounts``          chart-of-accounts entries; each has a type + currency.
* ``account_balances``  materialised running totals, updated atomically with
                        each posting (the *only* mutable ledger table).
* ``transactions``      append-only journal entries (immutable; see ledger_ddl).
* ``postings``          append-only debit/credit lines; sum(debits)==sum(credits)
                        per currency is enforced by the posting engine.

Amounts are stored as ``NUMERIC(38, 0)`` integer *minor units* — never floats.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# 38-digit integers comfortably hold even 18-decimal crypto balances.
Amount = Numeric(38, 0)


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _now()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Currency(Base):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    exponent: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("exponent >= 0 AND exponent <= 18", name="ck_currency_exponent"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        ForeignKey("currencies.code"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Attribute is `meta`; the DB column is `metadata` (the latter is reserved
    # by SQLAlchemy's declarative base, so it cannot be the attribute name).
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_accounts_tenant_external"),
        CheckConstraint(
            "type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
        Index("ix_accounts_tenant", "tenant_id"),
    )


class AccountBalance(Base):
    """Materialised per-account running totals.

    Maintained transactionally by the posting engine under a ``FOR UPDATE`` row
    lock. Storing debits and credits separately (rather than a single signed
    number) keeps the data side-agnostic; the signed balance is derived from the
    account's normal balance at read time.
    """

    __tablename__ = "account_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    currency_code: Mapped[str] = mapped_column(String(8), nullable=False)
    posted_debits: Mapped[int] = mapped_column(
        Amount, nullable=False, server_default="0", default=0
    )
    posted_credits: Mapped[int] = mapped_column(
        Amount, nullable=False, server_default="0", default=0
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Transaction(Base):
    """An immutable journal entry. Reversals are *new* transactions that point
    back at the original via ``reverses_transaction_id`` (unique, so a
    transaction can be reversed at most once)."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reverses_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    posted_at: Mapped[datetime] = _now()
    created_at: Mapped[datetime] = _now()

    postings: Mapped[list[Posting]] = relationship(
        back_populates="transaction",
        lazy="selectin",
        order_by="Posting.created_at",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_transactions_idempotency"
        ),
        UniqueConstraint(
            "reverses_transaction_id", name="uq_transactions_reverses"
        ),
        Index("ix_transactions_tenant_created", "tenant_id", "created_at"),
    )


class Posting(Base):
    """A single debit or credit line. ``amount`` is always a positive integer in
    minor units; the side is carried by ``direction``."""

    __tablename__ = "postings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[int] = mapped_column(Amount, nullable=False)
    currency_code: Mapped[str] = mapped_column(
        ForeignKey("currencies.code"), nullable=False
    )
    created_at: Mapped[datetime] = _now()

    transaction: Mapped[Transaction] = relationship(back_populates="postings")

    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')", name="ck_postings_direction"),
        CheckConstraint("amount > 0", name="ck_postings_amount_positive"),
        Index("ix_postings_account", "account_id"),
        Index("ix_postings_transaction", "transaction_id"),
    )
