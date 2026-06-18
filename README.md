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
MIT
