"""initial ledger schema

Revision ID: 0001
Revises:
Create Date: 2026-05-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.ledger_ddl import (
    CREATE_TRIGGERS,
    DEFAULT_CURRENCIES,
    DROP_FUNCTION,
    DROP_TRIGGERS,
    IMMUTABILITY_FUNCTION,
)

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 0)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    op.create_table(
        "currencies",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("exponent", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.CheckConstraint("exponent >= 0 AND exponent <= 18", name="ck_currency_exponent"),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("currency_code", sa.String(8), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_accounts_tenant_external"),
        sa.CheckConstraint(
            "type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
    )
    op.create_index("ix_accounts_tenant", "accounts", ["tenant_id"])

    op.create_table(
        "account_balances",
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("currency_code", sa.String(8), nullable=False),
        sa.Column("posted_debits", AMOUNT, server_default="0", nullable=False),
        sa.Column("posted_credits", AMOUNT, server_default="0", nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_account_balances_tenant_id", "account_balances", ["tenant_id"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=True),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("reverses_transaction_id", sa.Uuid(), sa.ForeignKey("transactions.id"), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_transactions_idempotency"),
        sa.UniqueConstraint("reverses_transaction_id", name="uq_transactions_reverses"),
    )
    op.create_index(
        "ix_transactions_tenant_created", "transactions", ["tenant_id", "created_at"]
    )

    op.create_table(
        "postings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("transaction_id", sa.Uuid(), sa.ForeignKey("transactions.id"), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("amount", AMOUNT, nullable=False),
        sa.Column("currency_code", sa.String(8), sa.ForeignKey("currencies.code"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("direction IN ('debit','credit')", name="ck_postings_direction"),
        sa.CheckConstraint("amount > 0", name="ck_postings_amount_positive"),
    )
    op.create_index("ix_postings_account", "postings", ["account_id"])
    op.create_index("ix_postings_transaction", "postings", ["transaction_id"])

    # Seed common currencies.
    op.bulk_insert(
        sa.table(
            "currencies",
            sa.column("code", sa.String),
            sa.column("exponent", sa.SmallInteger),
            sa.column("name", sa.String),
        ),
        [
            {"code": code, "exponent": exponent, "name": name}
            for code, exponent, name in DEFAULT_CURRENCIES
        ],
    )

    # Install append-only enforcement on the journal tables.
    op.execute(IMMUTABILITY_FUNCTION)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_TRIGGERS:
        op.execute(statement)
    op.execute(DROP_FUNCTION)

    op.drop_table("postings")
    op.drop_table("transactions")
    op.drop_table("account_balances")
    op.drop_table("accounts")
    op.drop_table("currencies")
    op.drop_table("api_keys")
    op.drop_table("tenants")
