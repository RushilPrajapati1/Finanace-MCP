# Finanace-MCP — FinLedger ledger engine

Finanace-MCP is the ledger engine for FinLedger. It implements a production-grade,
double-entry accounting core and exposes two programmatic interfaces:

- A REST API under /v1/* intended for UI and integrations.
- A Model Context Protocol (MCP) server for AI assistants and tools (e.g. Cursor,
  Claude) that need structured access to ledger data and helper tools.

This repository contains the backend engine only — no web UI. The companion
frontend (a Vite + React app) is in the Finance-Ledger-API repository and is a
REST client of this service.

Key points
- Correctness-first double-entry ledger (balanced per currency, append-only
  journal, idempotent posting).
- Exact-money arithmetic (integer minor units: `NUMERIC(38,0)`).
- Multi-tenant support via API keys (tenant-scoped data).
- Two interfaces: REST for integrations and MCP for assistant tooling.

Quick start (dev)
1. Start Postgres:
   make db-up
2. Install dev tools & dependencies:
   make dev
3. Apply migrations:
   make migrate
4. Create a tenant & copy the tenant key:
   finledger create-tenant "My Company"
5. Run the API:
   make run

Run with Docker:
   docker compose up --build

MCP server
- Enable the MCP features by configuring the tenant API key (from step 4).
- Start the MCP server locally:
  python -m app.mcp
  # or
  finledger-mcp

Security & secrets
- Never commit real secrets (tenant keys, DATABASE URLs, passwords) to the
  repository. Use platform secret stores (Render, Vercel, Neon) for production
  values and `.env` only in local, gitignored files.
- If any real keys have been committed in the past, rotate them immediately.

Deployment (summary)
- Backend container: Render (Docker). The repo includes `render.yaml`.
- Database: Neon (managed Postgres). Convert Neon connection strings for
  asyncpg as needed (see docs in this repo).
- Frontend: Finance-Ledger-API deployed separately (Vercel), communicates with
  this backend via `/v1/*` endpoints.

API & tools
- All `/v1/*` endpoints require an API key (`X-API-Key` or `Authorization: Bearer`).
- The MCP server exposes a set of helper tools (list_accounts, get_account_balance,
  get_trial_balance, verify_ledger_integrity, etc.) for assistant workflows.

Contributing & tests
- Tests run against a real Postgres instance (the ledger relies on triggers,
  locks, and exact-money rules).
  make db-up
  make test

License
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

**Read** — query the ledger:

| Tool | Purpose |
| --- | --- |
| `list_accounts` | Chart of accounts |
| `get_account_balance` | Balance for one account |
| `get_account_statement` | Posting history with running balances |
| `list_transactions` | Recent journal entries |
| `get_trial_balance` | Debit/credit totals per currency |
| `verify_ledger_integrity` | Detect balance drift |
| `get_portfolio_summary` | Net worth and P&L by currency |

**Write** — mutate the ledger:

| Tool | Purpose |
| --- | --- |
| `create_account` | Add a chart-of-accounts entry (idempotent on `external_id`) |
| `post_transaction` | Post a balanced transaction; `dry_run=True` previews without committing |
| `reverse_transaction` | Undo a posted transaction by posting its mirror |

The write tools are deliberately thin — every accounting rule lives in
`services/`/`domain/`, so the tools just translate agent input and pass it
through. A few choices worth calling out, because they're what makes these tools
safe to hand to an autonomous agent:

- **Agents can't create money.** `post_transaction` goes through the same posting
  engine as the REST API: a transaction needs ≥ 2 legs and debits must equal
  credits *per currency*, or it's rejected. The invariant is physical, not a
  prompt instruction.
- **Idempotency, because agents retry.** Pass an `idempotency_key` and replays
  return the original transaction. Omit one and the tool derives a deterministic
  key from the transaction's content, so an accidental retry can't double-post.
  (Trade-off: two *intentionally* identical transactions need distinct keys.)
- **Dry-run / confirmation.** `post_transaction(dry_run=True)` runs the full
  validation and returns the projected per-account balance impact **without
  committing** — so an agent (or a human in the loop) can confirm before taking
  an irreversible action. A clean dry-run guarantees the real post will pass.
- **Corrections are reversals, never edits.** Posted transactions are immutable;
  there is no edit/delete tool by design. `reverse_transaction` posts the mirror
  image — the agent respects immutability by construction.
- **Audit trail.** Agent-posted entries are stamped `actor="mcp-agent"` plus the
  API key and request origin, so they're distinguishable from human/REST
  activity. Errors come back with the ledger's stable `code` (e.g.
  `unbalanced_transaction`) for the agent to branch on.

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
