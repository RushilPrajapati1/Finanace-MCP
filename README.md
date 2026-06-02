# FinLedger

A backend framework that implements a **real double-entry accounting ledger**
for fintech companies. It is built to be correct first: balanced transactions,
immutable history, exact money, and multi-tenant isolation are enforced rather
than assumed.

- **Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · Alembic
- **Money:** stored as integer minor units (`NUMERIC(38,0)`) — never floats
- **Guarantees:** per-currency balanced postings, append-only journal (DB
  triggers), idempotent posting, reversals, materialised balances verified
  against the immutable history

---

## Why double-entry?

Every financial event touches at least two accounts: value leaves one place and
arrives in another. In double-entry bookkeeping that is recorded as **debits**
and **credits** that must sum to zero within each transaction. The five account
types and their *normal balance* (the side an increase is recorded on) are:

| Type      | Normal balance | Increases on |
|-----------|----------------|--------------|
| Asset     | Debit          | Debit        |
| Expense   | Debit          | Debit        |
| Liability | Credit         | Credit       |
| Equity    | Credit         | Credit       |
| Revenue   | Credit         | Credit       |

The accounting equation `assets + expenses = liabilities + equity + revenue`
holds at all times, which is why a correct ledger's **trial balance is always
zero** per currency. FinLedger enforces this on every write.

A customer depositing \$150 is a single balanced transaction:

```
DEBIT   Cash (asset)              150.00 USD   <- bank now holds the money
CREDIT  Customer Deposits (liab.) 150.00 USD   <- bank owes it to the customer
```

---

## Architecture

```
app/
  domain/        Pure accounting model — no DB, no web. Unit-testable rules.
    money.py       Money value object; decimal <-> integer minor units.
    enums.py       Account types, directions, normal-balance rules.
    errors.py      Domain errors (each maps to an HTTP status + code).
  models.py      SQLAlchemy schema (tenants, accounts, transactions, postings...).
  ledger_ddl.py  Append-only triggers + seed currencies (raw DDL).
  services/      Transactional use-cases. Own the commit/rollback boundary.
    ledger.py      The posting engine: validate -> balance -> write atomically.
    accounts.py    Chart-of-accounts management.
    balances.py    Balance reads, trial balance, integrity verification.
  api/           Thin FastAPI adapter over the services.
    deps.py        DB session + API-key auth.
    schemas.py     Pydantic request/response models (money as strings).
    routers/       accounts, transactions, ledger, health.
  cli.py         Operator CLI: create tenants / API keys.
migrations/      Alembic migrations (async).
tests/           Unit + integration + end-to-end tests.
```

The layering rule: **domain knows nothing of the database; the API knows nothing
of accounting rules.** All invariants live in `domain` and `services`, so they
hold no matter how a transaction is posted (HTTP, queue, CLI, or test).

### Correctness mechanisms

- **Balanced per currency.** The posting engine groups postings by currency and
  rejects any transaction where debits ≠ credits for a currency. Multi-currency
  transactions (e.g. FX) must balance each currency independently.
- **Immutable journal.** `transactions` and `postings` are append-only,
  enforced by PostgreSQL triggers that reject `UPDATE`/`DELETE`. Corrections are
  made by posting a **reversal**, never by editing history.
- **Exact money.** Amounts are integer minor units in `NUMERIC(38,0)`. The
  `Money` type refuses any amount finer than a currency permits (e.g. `1.005`
  USD, or fractional JPY).
- **Atomic balances.** A materialised `account_balances` row per account is
  updated under a `FOR UPDATE` row lock in the same transaction as the postings.
  Locks are taken in sorted account order to prevent deadlocks.
- **Idempotency.** A client-supplied `idempotency_key` (unique per tenant) makes
  posting safe to retry; replays return the original transaction.
- **Multi-tenancy.** Every row carries a `tenant_id`; the API key resolves the
  tenant and all queries are scoped to it.
- **Auditability.** `GET /v1/ledger/verify` recomputes balances from the raw
  posting history and reports any drift from the materialised totals.

---

## Quick start

```bash
# 1. Start Postgres
make db-up

# 2. Install (editable, with dev tools)
make dev

# 3. Apply migrations
make migrate

# 4. Create a tenant + API key (copy the key it prints)
finledger create-tenant "Acme Payments"

# 5. Run the API (http://localhost:8000/docs for Swagger UI)
make run
```

Or run the whole stack in Docker (migrations run automatically on boot):

```bash
docker compose up --build
```

---

## API walkthrough

All `/v1/*` endpoints require an API key via `X-API-Key` or
`Authorization: Bearer`.

```bash
KEY=sk_live_xxx
BASE=http://localhost:8000

# Create two accounts
CASH=$(curl -s $BASE/v1/accounts -H "X-API-Key: $KEY" \
  -d '{"name":"Cash","type":"asset","currency":"USD"}' | jq -r .id)
DEP=$(curl -s $BASE/v1/accounts -H "X-API-Key: $KEY" \
  -d '{"name":"Customer Deposits","type":"liability","currency":"USD"}' | jq -r .id)

# Post a balanced deposit (idempotent via idempotency_key)
curl -s $BASE/v1/transactions -H "X-API-Key: $KEY" -d "{
  \"description\": \"customer deposit\",
  \"idempotency_key\": \"dep-001\",
  \"postings\": [
    {\"account_id\": \"$CASH\", \"direction\": \"debit\",  \"amount\": \"150.00\"},
    {\"account_id\": \"$DEP\",  \"direction\": \"credit\", \"amount\": \"150.00\"}
  ]
}"

# Read a balance
curl -s $BASE/v1/accounts/$CASH/balance -H "X-API-Key: $KEY"

# Trial balance for the whole tenant (balanced == true on a healthy ledger)
curl -s $BASE/v1/ledger/trial-balance -H "X-API-Key: $KEY"

# Reverse a transaction
curl -s $BASE/v1/transactions/<txn_id>/reversal -H "X-API-Key: $KEY" -d '{}'
```

### Endpoints

| Method | Path                                  | Purpose                          |
|--------|---------------------------------------|----------------------------------|
| POST   | `/v1/accounts`                        | Create an account                |
| GET    | `/v1/accounts`                        | List accounts                    |
| GET    | `/v1/accounts/{id}`                   | Fetch an account                 |
| GET    | `/v1/accounts/{id}/balance`           | Account balance                  |
| POST   | `/v1/transactions`                    | Post a balanced transaction      |
| GET    | `/v1/transactions`                    | List transactions                |
| GET    | `/v1/transactions/{id}`               | Fetch a transaction              |
| POST   | `/v1/transactions/{id}/reversal`      | Reverse a transaction            |
| GET    | `/v1/ledger/trial-balance`            | Per-currency debit/credit totals |
| GET    | `/v1/ledger/verify`                   | Verify balances vs. history      |
| GET    | `/health`, `/health/ready`            | Liveness / readiness             |

---

## Testing

Tests run against a real PostgreSQL database (the ledger depends on triggers,
row locks, and `NUMERIC`).

```bash
make db-up
PGPASSWORD=finledger psql -h localhost -U finledger -d finledger \
  -c "CREATE DATABASE finledger_test;"
make test
```

Coverage includes exact-money rules, the balanced-per-currency invariant,
idempotency, reversals, append-only enforcement, and full HTTP flows.

---

## Extending the framework

- **New currencies:** insert into `currencies` (or extend `DEFAULT_CURRENCIES`
  in `app/ledger_ddl.py`) with the correct `exponent`.
- **Account holds / available balance:** add `pending_*` columns to
  `account_balances` and a two-phase (authorize → capture) flow in the engine.
- **Effective dating / backdating:** `transactions.posted_at` is already
  separate from `created_at`; expose it on the API and report on it.
- **Event streaming:** emit a domain event after `post_transaction` commits to
  publish an append-only feed (Kafka/SNS) for downstream systems.
- **Per-request transactions:** services currently own their commit; swap to a
  unit-of-work dependency if you need to compose multiple use-cases atomically.

---

## License

MIT
