.PHONY: help install dev db-up db-down migrate downgrade revision run test lint format up mcp-install mcp-dev

DB_URL ?= postgresql+asyncpg://finledger:finledger@localhost:5432/finledger
TEST_DB_URL ?= postgresql+asyncpg://finledger:finledger@localhost:5432/finledger_test

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	pip install .

dev:  ## Install dev + runtime dependencies
	pip install -e ".[dev,mcp]"

mcp-install:  ## Install MCP server dependencies only
	pip install -e ".[mcp]"

mcp-dev:  ## Run MCP server with Inspector (needs FINLEDGER_API_KEY)
	mcp dev app/mcp/server.py

db-up:  ## Start a local Postgres via docker compose
	docker compose up -d db

db-down:  ## Stop the local Postgres
	docker compose down

migrate:  ## Apply migrations to the dev database
	FINLEDGER_DATABASE_URL=$(DB_URL) alembic upgrade head

downgrade:  ## Roll back the last migration
	FINLEDGER_DATABASE_URL=$(DB_URL) alembic downgrade -1

revision:  ## Autogenerate a migration: make revision m="message"
	FINLEDGER_DATABASE_URL=$(DB_URL) alembic revision --autogenerate -m "$(m)"

run:  ## Run the API server with autoreload
	FINLEDGER_DATABASE_URL=$(DB_URL) uvicorn app.main:app --reload

test:  ## Run the test suite (needs the test database to exist)
	FINLEDGER_TEST_DATABASE_URL=$(TEST_DB_URL) pytest -q

lint:  ## Lint with ruff
	ruff check app tests

format:  ## Auto-format with ruff
	ruff check --fix app tests

up:  ## Build and start the full stack (db + api)
	docker compose up --build
