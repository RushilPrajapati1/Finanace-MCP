"""Application configuration, loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every field can be overridden by a ``FINLEDGER_*`` env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FINLEDGER_",
        extra="ignore",
    )

    app_name: str = "FinLedger"
    environment: str = "development"

    # postgresql+asyncpg://user:password@host:port/database
    database_url: str = (
        "postgresql+asyncpg://finledger:finledger@localhost:5432/finledger"
    )

    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # Only honour X-Forwarded-For when the app sits behind a trusted reverse
    # proxy (Render, nginx, ...). When false, the header is ignored and the
    # direct peer address is used, so clients cannot forge the audit source_ip.
    trust_proxy_headers: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
