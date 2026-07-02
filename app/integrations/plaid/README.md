# Plaid → FinLedger connector (sandbox demo)

Mirrors the transactions of bank/credit accounts linked through
[Plaid](https://plaid.com) into a FinLedger tenant as balanced double-entry
transactions. Same pipeline shape as the Open Collective connector:

```
fetch (Plaid /transactions/sync) → map (signed row → double entry) → load (/v1)
```

## Mapping rules

Plaid reports each transaction from the linked account's perspective as one
signed row (positive = money out, negative = money in). Every settled row
becomes a two-leg FinLedger transaction:

| Plaid row            | Debit                  | Credit                 |
| -------------------- | ---------------------- | ---------------------- |
| amount > 0 (outflow) | `Expenses:<category>`  | linked account         |
| amount < 0 (inflow)  | linked account         | `Income:<category>`    |

* The linked account's ledger type follows Plaid's account type:
  `depository`/`investment` → **asset**, `credit`/`loan` → **liability** — so a
  credit-card purchase correctly grows the liability and a repayment shrinks it.
* `<category>` comes from Plaid's `personal_finance_category.primary`.
* **Idempotency:** `plaid:<transaction_id>` is both the `idempotency_key` and
  `external_id`, so re-runs and overlapping sync windows post nothing new.
  Accounts are idempotent on `external_id` too.
* **Pending** transactions are skipped (Plaid re-delivers them with a new id
  once settled). **Removed** transactions are reported to stderr — an
  append-only ledger corrects by reversal, which is an operator decision.
* Amounts are converted `Decimal(str(x))` and quantized to the ISO exponent —
  no floats reach the ledger. Unsupported currencies are skipped.

## Running the sandbox demo

1. Get free sandbox keys at <https://dashboard.plaid.com> (Team Settings → Keys).
2. Install the extra and set the environment:

   ```bash
   pip install -e ".[integrations]"
   export PLAID_CLIENT_ID=...
   export PLAID_SECRET=...          # the *sandbox* secret
   ```

3. Preview without writing anything (bootstraps a fake "First Platypus Bank"
   item and prints the mapped entries):

   ```bash
   python -m app.integrations.plaid --dry-run
   ```

4. Load into a FinLedger tenant:

   ```bash
   python -m app.integrations.plaid \
       --base-url http://localhost:8000 \
       --api-key sk_live_...
   ```

The run prints the item's `access_token` and the sync `cursor`; pass them back
with `--access-token`/`--cursor` for cheap incremental re-runs:

```bash
python -m app.integrations.plaid --access-token access-sandbox-... --cursor <cursor> ...
```

Outside the sandbox, items are linked through
[Plaid Link](https://plaid.com/docs/link/) in your own UI; pass the resulting
`--access-token` (the bootstrap shortcut is sandbox-only).
