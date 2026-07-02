"""tenant-scoped referential integrity (defense in depth)

Security hardening. The single-column foreign keys let the *database* accept
rows that cross the tenant boundary — a posting pointing at another tenant's
account or transaction, or a reversal pointing at another tenant's transaction.
The application layer always scopes these lookups by ``tenant_id``, but for a
multi-tenant financial ledger the database must be the backstop (the same
philosophy as the append-only triggers): no application bug and no ad-hoc
INSERT at a psql prompt may violate tenant isolation.

This migration replaces the single-column FKs with composite, tenant-scoped
ones:

  * ``postings (transaction_id, tenant_id)  -> transactions (id, tenant_id)``
  * ``postings (account_id, tenant_id)      -> accounts (id, tenant_id)``
  * ``transactions (reverses_transaction_id, tenant_id)
                                            -> transactions (id, tenant_id)``
  * ``account_balances (account_id, tenant_id) -> accounts (id, tenant_id)``

and adds the previously missing ``account_balances.currency_code`` FK.

The composite FK targets require ``UNIQUE (id, tenant_id)`` on ``accounts``
and ``transactions`` (redundant with the PK for lookups, but required as an
FK anchor).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-02
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Composite FK targets.
    op.create_unique_constraint("uq_accounts_id_tenant", "accounts", ["id", "tenant_id"])
    op.create_unique_constraint(
        "uq_transactions_id_tenant", "transactions", ["id", "tenant_id"]
    )

    # Drop the tenant-blind single-column FKs (created unnamed in 0001, so they
    # carry PostgreSQL's default names).
    op.drop_constraint("postings_transaction_id_fkey", "postings", type_="foreignkey")
    op.drop_constraint("postings_account_id_fkey", "postings", type_="foreignkey")
    op.drop_constraint(
        "transactions_reverses_transaction_id_fkey", "transactions", type_="foreignkey"
    )
    op.drop_constraint(
        "account_balances_account_id_fkey", "account_balances", type_="foreignkey"
    )

    # Recreate them tenant-scoped.
    op.create_foreign_key(
        "fk_postings_transaction_tenant",
        "postings",
        "transactions",
        ["transaction_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_postings_account_tenant",
        "postings",
        "accounts",
        ["account_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_transactions_reverses_tenant",
        "transactions",
        "transactions",
        ["reverses_transaction_id", "tenant_id"],
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_account_balances_account_tenant",
        "account_balances",
        "accounts",
        ["account_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )

    # Previously missing: balance rows could carry an unregistered currency.
    op.create_foreign_key(
        "fk_account_balances_currency",
        "account_balances",
        "currencies",
        ["currency_code"],
        ["code"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_account_balances_currency", "account_balances", type_="foreignkey")
    op.drop_constraint(
        "fk_account_balances_account_tenant", "account_balances", type_="foreignkey"
    )
    op.drop_constraint("fk_transactions_reverses_tenant", "transactions", type_="foreignkey")
    op.drop_constraint("fk_postings_account_tenant", "postings", type_="foreignkey")
    op.drop_constraint("fk_postings_transaction_tenant", "postings", type_="foreignkey")

    op.create_foreign_key(
        "postings_transaction_id_fkey", "postings", "transactions", ["transaction_id"], ["id"]
    )
    op.create_foreign_key(
        "postings_account_id_fkey", "postings", "accounts", ["account_id"], ["id"]
    )
    op.create_foreign_key(
        "transactions_reverses_transaction_id_fkey",
        "transactions",
        "transactions",
        ["reverses_transaction_id"],
        ["id"],
    )
    op.create_foreign_key(
        "account_balances_account_id_fkey",
        "account_balances",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("uq_transactions_id_tenant", "transactions", type_="unique")
    op.drop_constraint("uq_accounts_id_tenant", "accounts", type_="unique")
