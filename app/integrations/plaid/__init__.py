"""Plaid -> FinLedger connector.

Mirrors the transactions of bank/credit accounts linked through Plaid
(https://plaid.com) into a FinLedger tenant as balanced double-entry
transactions. Follows the same pipeline shape as the Open Collective connector:

    fetch (Plaid /transactions/sync) -> map (signed row -> double entry) -> load (/v1)

Run the sandbox demo with::

    python -m app.integrations.plaid --dry-run

See ``README.md`` in this directory for setup and the mapping rules.
"""

from app.integrations.plaid.client import PlaidClient, PlaidError
from app.integrations.plaid.mapper import (
    ledger_account_for,
    map_transaction,
)

__all__ = [
    "PlaidClient",
    "PlaidError",
    "ledger_account_for",
    "map_transaction",
]
