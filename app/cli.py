"""Operator CLI: bootstrap tenants and API keys.

    finledger create-tenant "Acme Payments"
    finledger create-key <tenant_id> --name backend
    finledger list-tenants
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.db import SessionLocal
from app.models import ApiKey, Tenant
from app.security import generate_api_key


async def _create_tenant(name: str) -> None:
    async with SessionLocal() as session:
        tenant = Tenant(name=name)
        session.add(tenant)
        await session.flush()

        raw, key_hash, prefix = generate_api_key()
        session.add(
            ApiKey(tenant_id=tenant.id, name="default", prefix=prefix, key_hash=key_hash)
        )
        await session.commit()

    print(f"Tenant created: {tenant.id}  ({name})")
    print(f"API key (shown once): {raw}")


async def _create_key(tenant_id: uuid.UUID, name: str) -> None:
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise SystemExit(f"no such tenant: {tenant_id}")
        raw, key_hash, prefix = generate_api_key()
        session.add(
            ApiKey(tenant_id=tenant.id, name=name, prefix=prefix, key_hash=key_hash)
        )
        await session.commit()
    print(f"API key (shown once): {raw}")


async def _list_tenants() -> None:
    async with SessionLocal() as session:
        tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    if not tenants:
        print("(no tenants)")
        return
    for tenant in tenants:
        print(f"{tenant.id}  {tenant.name}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="finledger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_tenant = sub.add_parser("create-tenant", help="Create a tenant and a first API key")
    p_tenant.add_argument("name")

    p_key = sub.add_parser("create-key", help="Mint an additional API key for a tenant")
    p_key.add_argument("tenant_id", type=uuid.UUID)
    p_key.add_argument("--name", default="default")

    sub.add_parser("list-tenants", help="List tenants")

    args = parser.parse_args()
    if args.command == "create-tenant":
        asyncio.run(_create_tenant(args.name))
    elif args.command == "create-key":
        asyncio.run(_create_key(args.tenant_id, args.name))
    elif args.command == "list-tenants":
        asyncio.run(_list_tenants())


if __name__ == "__main__":
    main()
