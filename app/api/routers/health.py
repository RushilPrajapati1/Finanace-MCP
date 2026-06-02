"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import SessionDep
from app.api.schemas import HealthOut
from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    settings = get_settings()
    return HealthOut(
        status="ok", service=settings.app_name, environment=settings.environment
    )


@router.get("/health/ready", response_model=HealthOut)
async def ready(session: SessionDep) -> HealthOut:
    """Readiness probe: confirms the database is reachable."""
    await session.execute(text("SELECT 1"))
    settings = get_settings()
    return HealthOut(
        status="ready", service=settings.app_name, environment=settings.environment
    )
