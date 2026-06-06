"""posting running-balance snapshot

Adds ``balance_before`` / ``balance_after`` to ``postings`` — the account's
signed balance (minor units) immediately before and after each line was applied.

Existing rows are backfilled with a per-account cumulative sum over the posting
history. The append-only triggers block UPDATEs, so they are lifted *only* for
the duration of the backfill and then reinstalled.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-06
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.ledger_ddl import CREATE_TRIGGERS, DROP_TRIGGERS

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(38, 0)

# Recompute the running signed balance for every existing posting. The signed
# delta of a line depends on the account's normal-balance side:
#   debit-normal  (asset, expense)              -> debit increases, credit decreases
#   credit-normal (liability, equity, revenue)  -> credit increases, debit decreases
# A window cumulative sum per account (ordered by created_at, then id as a stable
# tiebreak) yields balance_after; balance_before is that minus the line's delta.
_BACKFILL = """
WITH signed AS (
    SELECT
        p.id,
        p.account_id,
        p.created_at,
        CASE
            WHEN (a.type IN ('asset', 'expense') AND p.direction = 'debit')
              OR (a.type IN ('liability', 'equity', 'revenue') AND p.direction = 'credit')
            THEN p.amount
            ELSE -p.amount
        END AS delta
    FROM postings p
    JOIN accounts a ON a.id = p.account_id
),
running AS (
    SELECT
        id,
        delta,
        SUM(delta) OVER (
            PARTITION BY account_id
            ORDER BY created_at, id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS balance_after
    FROM signed
)
UPDATE postings p
SET balance_after = r.balance_after,
    balance_before = r.balance_after - r.delta
FROM running r
WHERE p.id = r.id;
"""


def upgrade() -> None:
    # Add nullable first so existing rows can be backfilled before NOT NULL.
    op.add_column("postings", sa.Column("balance_before", AMOUNT, nullable=True))
    op.add_column("postings", sa.Column("balance_after", AMOUNT, nullable=True))

    # The immutability triggers reject UPDATEs; lift them just to backfill the
    # snapshot onto historical rows, then reinstall (the trigger function itself
    # is left in place by migration 0001).
    for statement in DROP_TRIGGERS:
        op.execute(statement)
    op.execute(_BACKFILL)
    for statement in CREATE_TRIGGERS:
        op.execute(statement)

    op.alter_column("postings", "balance_before", nullable=False)
    op.alter_column("postings", "balance_after", nullable=False)


def downgrade() -> None:
    op.drop_column("postings", "balance_after")
    op.drop_column("postings", "balance_before")
