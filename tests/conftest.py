"""Test fixtures.

These tests run against a *real* PostgreSQL database (the ledger relies on
Postgres-specific behaviour — triggers, ``FOR UPDATE`` locks, ``NUMERIC``).
Point ``FINLEDGER_TEST_DATABASE_URL`` at a disposable database; it defaults to
``finledger_test`` on localhost.

A NullPool engine is used throughout so that no connection is ever shared
between event loops, which keeps schema setup (run in its own loop) and the
per-test loops cleanly isolated.
"""

from __future__ import annotations

import asyncio
import os

# Must be set before any `app.*` import so Settings picks up the test database.
_TEST_URL = os.environ.get(
    "FINLEDGER_TEST_DATABASE_URL",
    "postgresql+asyncpg://finledger:finledger@localhost:5432/finledger_test",
)
os.environ["FINLEDGER_DATABASE_URL"] = _TEST_URL

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db import get_session  # noqa: E402
from app.ledger_ddl import DEFAULT_CURRENCIES, apply_immutability_sql  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ApiKey, Base, Currency, Tenant  # noqa: E402
from app.security import generate_api_key  # noqa: E402

test_engine = create_async_engine(_TEST_URL, poolclass=NullPool)
TestSession = async_sessionmaker(test_engine, expire_on_commit=False)

_TABLES = (
    "postings",
    "transactions",
    "account_balances",
    "accounts",
    "api_keys",
    "tenants",
)


async def _create_schema() -> None:
    engine = create_async_engine(_TEST_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        for statement in apply_immutability_sql():
            await conn.execute(text(statement))
        # The migration seeds currencies; create_all does not, so seed here too.
        await conn.execute(
            Currency.__table__.insert(),
            [
                {"code": code, "exponent": exponent, "name": name}
                for code, exponent, name in DEFAULT_CURRENCIES
            ],
        )
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    asyncio.run(_create_schema())


async def _override_get_session():
    async with TestSession() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[get_session] = _override_get_session


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate ledger tables after each test.

    TRUNCATE does not fire the row-level append-only triggers, so it is the
    right tool for resetting the immutable journal between tests.
    """
    yield
    async with test_engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def session():
    async with TestSession() as s:
        yield s


@pytest_asyncio.fixture
async def tenant(session) -> Tenant:
    t = Tenant(name="Test Co")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@pytest_asyncio.fixture
async def auth() -> dict:
    """Create a tenant + API key and return request headers and the tenant id."""
    raw, key_hash, prefix = generate_api_key()
    async with TestSession() as s:
        t = Tenant(name="API Test Co")
        s.add(t)
        await s.flush()
        s.add(ApiKey(tenant_id=t.id, name="default", prefix=prefix, key_hash=key_hash))
        await s.commit()
        tenant_id = t.id
    return {"headers": {"X-API-Key": raw}, "tenant_id": tenant_id}


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
