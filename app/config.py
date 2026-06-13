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

    # Streamable-HTTP MCP transport, mounted at /mcp on the API. The MCP SDK
    # applies DNS-rebinding protection that validates the Host/Origin of every
    # incoming MCP request; by default only localhost is allowed. When the
    # server is reachable at a public domain, list the host(s) and origin(s)
    # here (comma-separated) so MCP clients are not rejected with 421, e.g.
    #   FINLEDGER_MCP_ALLOWED_HOSTS="finledger.onrender.com"
    #   FINLEDGER_MCP_ALLOWED_ORIGINS="https://finledger.onrender.com"
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    @property
    def mcp_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]

    @property
    def mcp_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.mcp_allowed_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached ``Settings`` instance."""
    return Settings()
