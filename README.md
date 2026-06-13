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

## MCP server (AI assistants)

FinLedger exposes a **Model Context Protocol** server so tools like Cursor and
Claude can query balances, transaction history, trial balance, portfolio
rollups, and ledger integrity in natural language.

### Setup

```bash
# 1. Backend prerequisites (Postgres + migrations + tenant key)
make db-up
make dev          # installs mcp[cli] alongside dev deps
make migrate
finledger create-tenant "My Company"   # copy the API key

# 2. Configure the key (copy .env.example → .env, or export directly)
export FINLEDGER_API_KEY="sk_live_..."
export FINLEDGER_DATABASE_URL="postgresql+asyncpg://finledger:finledger@localhost:5432/finledger"

# 3. Test with MCP Inspector
make mcp-dev
```

### Cursor integration

The workspace ships `.cursor/mcp.json`. After creating your tenant key:

1. Edit `.cursor/mcp.json` and replace `PASTE_YOUR_TENANT_API_KEY_HERE`
2. Create a venv in `Finanace-MCP` if you have not already:
   ```bash
   cd Finanace-MCP
   python -m venv .venv
   .venv\Scripts\activate        # Windows
   make dev
   ```
3. Restart Cursor (or reload MCP servers in settings)

### MCP tools

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Chart of accounts |
| `get_account_balance` | Balance for one account |
| `get_account_statement` | Posting history with running balances |
| `list_transactions` | Recent journal entries |
| `get_trial_balance` | Debit/credit totals per currency |
| `verify_ledger_integrity` | Detect balance drift |
| `get_portfolio_summary` | Net worth and P&L by currency |

Run directly (stdio):

```bash
python -m app.mcp
# or
finledger-mcp
```

---

## Deployment (production)

FinLedger runs as a **long-lived container against managed Postgres** — not
serverless. The live free-tier stack:

| Tier     | Platform            | Notes                                          |
|----------|---------------------|------------------------------------------------|
| API      | **Render** (Docker) | Builds this repo's `Dockerfile`; free plan     |
| Database | **Neon** (Postgres) | Managed, point-in-time recovery, scale-to-zero |
| Frontend | **Vercel** (`web/`) | Static Vite build; reaches the API via rewrite |

### 1. Database — Neon

Create a Neon project and copy its connection string, then convert it for this
app's async driver. **Three edits, all required:**

- scheme `postgresql://` → `postgresql+asyncpg://`
- use the **direct** endpoint (drop `-pooler` from the host) — the app keeps its
  own SQLAlchemy pool, and Neon's pooled PgBouncer endpoint breaks asyncpg's
  prepared statements
- replace the query `?sslmode=require&channel_binding=require` → `?ssl=require`
  (asyncpg understands neither `sslmode` nor `channel_binding`)

```
postgresql+asyncpg://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/neondb?ssl=require
```

> A mangled host (e.g. a newline from a wrapped copy-paste) surfaces as
> `SSLV3_ALERT_ILLEGAL_PARAMETER` — Neon routes by SNI, so a broken hostname is
> rejected during the TLS handshake, not as a connect error.

### 2. Backend — Render

A `render.yaml` Blueprint is included. In Render → **New → Blueprint**, point it
at this repo; it provisions a Docker web service (free plan, health check
`GET /health`, listens on `PORT=8000`). Set the one secret in the dashboard (it
is `sync: false` in the blueprint):

```
FINLEDGER_DATABASE_URL = <the converted asyncpg URL from step 1>
```

> **Gotcha:** paste the **converted** URL, not Neon's raw `postgresql://` string.
> The raw form makes SQLAlchemy fall back to psycopg2 →
> `ModuleNotFoundError: No module named 'psycopg2'` at the migration step.

The container entrypoint runs `alembic upgrade head` on every boot, so migrations
apply automatically as the release step. Mint a tenant + key from the Render
**Shell**:

```bash
finledger create-tenant "My Company"   # copy the sk_live_… (shown once)
```

### 3. Frontend — Vercel

The `web/` app calls `/api/*`, and `web/vercel.json` rewrites that to the Render
URL — so the browser stays **same-origin** and the backend needs no CORS. Import
`web/` into Vercel (Vite is auto-detected), set **no environment variables** (the
API key is entered in the app's Settings screen, never baked into the public
bundle), and deploy. Paste the `sk_live_…` key into Settings to connect.

### Secrets & ops

- Keep `FINLEDGER_DATABASE_URL` and API keys in the platform secret stores, never
  in the repo. Rotate any secret that has ever been pasted into a chat/log.
- Render's free plan **sleeps after ~15 min idle** — the first request then takes
  ~30–50s (cold start). Upgrade the service to keep it always-on.
- Pushing to `main` auto-deploys both tiers; schema changes ship by committing a
  new Alembic migration (Render applies it on the next deploy).

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
