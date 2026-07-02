"""Plaid sandbox demo: link a fake bank, sync its history, post it to FinLedger.

With only Plaid sandbox credentials this bootstraps a sandbox item (Plaid's
"First Platypus Bank"), drains ``/transactions/sync``, maps every settled row
to a balanced double entry, and loads it into a FinLedger tenant (or just
prints the mapping with ``--dry-run``). Pass ``--access-token`` to reuse an
item across runs — combined with ``--cursor`` the run is incremental, and the
ledger's idempotency makes overlaps harmless either way.

Usage::

    export PLAID_CLIENT_ID=... PLAID_SECRET=...       # sandbox keys
    python -m app.integrations.plaid --dry-run        # preview, write nothing

    python -m app.integrations.plaid \\
        --base-url http://localhost:8000 --api-key sk_live_...
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from app.integrations.opencollective import FinLedgerClient
from app.integrations.plaid.client import SANDBOX_INSTITUTION, PlaidClient
from app.integrations.plaid.mapper import ledger_account_for, map_transaction


@dataclass(slots=True)
class SyncSummary:
    fetched: int = 0
    posted: int = 0
    skipped: int = 0
    removed: int = 0
    errors: int = 0


def run_sync(
    *,
    plaid: PlaidClient,
    access_token: str,
    cursor: str | None = None,
    base_url: str = "",
    api_key: str = "",
    max_count: int | None = None,
    dry_run: bool = False,
) -> tuple[SyncSummary, str | None]:
    """Sync one Plaid item into FinLedger. Returns ``(summary, next_cursor)``."""
    summary = SyncSummary()

    ledger_accounts = {}
    for plaid_account in plaid.get_accounts(access_token):
        spec = ledger_account_for(plaid_account)
        if spec is not None:
            ledger_accounts[plaid_account["account_id"]] = spec

    added, removed, next_cursor = plaid.sync_all_transactions(access_token, cursor)
    if max_count is not None:
        added = added[:max_count]

    # A removed transaction cannot be deleted from an append-only ledger; the
    # correction is a reversal, which needs an operator's judgement. Surface them.
    summary.removed = len(removed)
    for row in removed:
        print(
            f"  REMOVED upstream: plaid:{row.get('transaction_id')} — "
            "post a reversal in FinLedger if it was already imported",
            file=sys.stderr,
        )

    mapped_txns = []
    for txn in added:
        summary.fetched += 1
        mapped = map_transaction(txn, ledger_accounts)
        if mapped is None:
            summary.skipped += 1
            continue
        mapped_txns.append(mapped)

    if dry_run:
        for mapped in mapped_txns:
            summary.posted += 1  # "would post"
            print(
                f"  {mapped.amount:>12} {mapped.currency}  "
                f"{mapped.debit.name} <- {mapped.credit.name}   "
                f"[{mapped.idempotency_key}] {mapped.description[:40]}"
            )
        return summary, next_cursor

    with FinLedgerClient(base_url, api_key) as ledger:
        for mapped in mapped_txns:
            status, body = ledger.post_transaction(mapped)
            if status == "posted":
                summary.posted += 1
            else:
                summary.errors += 1
                err = body.get("error", body)
                print(
                    f"  ERROR {mapped.idempotency_key}: "
                    f"{err.get('code')} — {err.get('message')}",
                    file=sys.stderr,
                )
    return summary, next_cursor


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="finledger-import-plaid",
        description="Mirror a Plaid item's transactions into FinLedger "
        "(bootstraps a sandbox item when no --access-token is given).",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("PLAID_CLIENT_ID"),
        help="Plaid client id (default: $PLAID_CLIENT_ID)",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("PLAID_SECRET"),
        help="Plaid secret (default: $PLAID_SECRET)",
    )
    parser.add_argument(
        "--plaid-env",
        default=os.environ.get("PLAID_ENV", "sandbox"),
        choices=["sandbox", "production"],
        help="Plaid environment (default: $PLAID_ENV or sandbox)",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("PLAID_ACCESS_TOKEN"),
        help="Reuse an existing item instead of bootstrapping a sandbox one",
    )
    parser.add_argument(
        "--cursor", default=None, help="Resume /transactions/sync from this cursor"
    )
    parser.add_argument(
        "--institution",
        default=SANDBOX_INSTITUTION,
        help=f"Sandbox institution to link (default: {SANDBOX_INSTITUTION})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FINLEDGER_BASE_URL", "http://localhost:8000"),
        help="FinLedger API base URL (default: $FINLEDGER_BASE_URL or localhost)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FINLEDGER_API_KEY"),
        help="FinLedger tenant API key (default: $FINLEDGER_API_KEY)",
    )
    parser.add_argument(
        "--max", type=int, default=None, help="Cap the number of transactions imported"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and map, printing what would post — write nothing",
    )
    args = parser.parse_args()

    if not args.client_id or not args.secret:
        parser.error(
            "Plaid credentials are required: set $PLAID_CLIENT_ID and $PLAID_SECRET "
            "(free sandbox keys: https://dashboard.plaid.com)"
        )
    if not args.dry_run and not args.api_key:
        parser.error("--api-key (or $FINLEDGER_API_KEY) is required unless --dry-run")
    if args.access_token and args.plaid_env == "sandbox" and not args.access_token.startswith(
        "access-sandbox-"
    ):
        parser.error("--access-token does not look like a sandbox token")

    with PlaidClient(args.client_id, args.secret, args.plaid_env) as plaid:
        access_token = args.access_token
        if not access_token:
            if args.plaid_env != "sandbox":
                parser.error(
                    "--access-token is required outside the sandbox (items are "
                    "linked through Plaid Link, not bootstrapped)"
                )
            print(f"Linking sandbox institution {args.institution} ...")
            public_token = plaid.sandbox_create_public_token(args.institution)
            access_token = plaid.exchange_public_token(public_token)
            print(f"Sandbox item ready. access_token={access_token}")

        print(
            f"Syncing Plaid transactions "
            f"{'(dry run) ' if args.dry_run else f'-> {args.base_url} '}..."
        )
        summary, next_cursor = run_sync(
            plaid=plaid,
            access_token=access_token,
            cursor=args.cursor,
            base_url=args.base_url,
            api_key=args.api_key or "",
            max_count=args.max,
            dry_run=args.dry_run,
        )

    print(
        f"\nDone. fetched={summary.fetched} "
        f"{'would_post' if args.dry_run else 'posted'}={summary.posted} "
        f"skipped={summary.skipped} removed_upstream={summary.removed} "
        f"errors={summary.errors}"
    )
    if next_cursor:
        print(f"Next incremental run: --access-token {access_token} --cursor {next_cursor}")


if __name__ == "__main__":
    main()
