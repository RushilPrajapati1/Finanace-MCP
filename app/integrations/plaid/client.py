"""Minimal Plaid REST client (httpx — no plaid-python dependency).

Covers exactly what the connector needs: sandbox item bootstrap, token
exchange, account listing, and the ``/transactions/sync`` cursor loop.
Credentials are injected into every request body, which is how Plaid's API
authenticates (there is no Authorization header).
"""

from __future__ import annotations

import sys
import time
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - guidance when the extra is absent
    print(
        "This connector needs httpx. Install it with:\n"
        '    pip install -e ".[integrations]"',
        file=sys.stderr,
    )
    raise

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

# Plaid's default sandbox institution ("First Platypus Bank"): linking it
# instantly yields a set of checking/savings/credit accounts with seeded
# transaction history — ideal for a demo without touching a real bank.
SANDBOX_INSTITUTION = "ins_109508"


class PlaidError(RuntimeError):
    """A structured Plaid API error (carries the stable ``error_code``)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


class PlaidClient:
    def __init__(
        self,
        client_id: str,
        secret: str,
        environment: str = "sandbox",
        *,
        timeout: float = 30.0,
    ):
        if environment not in PLAID_HOSTS:
            raise ValueError(
                f"unknown Plaid environment {environment!r}; "
                f"use one of {sorted(PLAID_HOSTS)}"
            )
        self._client_id = client_id
        self._secret = secret
        self._client = httpx.Client(base_url=PLAID_HOSTS[environment], timeout=timeout)

    def __enter__(self) -> PlaidClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict:
        body = {"client_id": self._client_id, "secret": self._secret, **payload}
        resp = self._client.post(path, json=body)
        data = resp.json()
        if resp.is_error or data.get("error_code"):
            raise PlaidError(
                data.get("error_code", f"HTTP_{resp.status_code}"),
                data.get("error_message", resp.text),
            )
        return data

    # ------------------------------------------------------------------ #
    # Sandbox bootstrap
    # ------------------------------------------------------------------ #
    def sandbox_create_public_token(
        self,
        institution_id: str = SANDBOX_INSTITUTION,
        products: tuple[str, ...] = ("transactions",),
    ) -> str:
        """Link a sandbox institution without the Link UI; returns a public_token."""
        data = self._post(
            "/sandbox/public_token/create",
            {"institution_id": institution_id, "initial_products": list(products)},
        )
        return data["public_token"]

    def exchange_public_token(self, public_token: str) -> str:
        """Trade a public_token for the long-lived access_token."""
        data = self._post("/item/public_token/exchange", {"public_token": public_token})
        return data["access_token"]

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def get_accounts(self, access_token: str) -> list[dict]:
        """The item's accounts (name, type, currency, balances)."""
        return self._post("/accounts/get", {"access_token": access_token})["accounts"]

    def sync_transactions(
        self, access_token: str, cursor: str | None = None, count: int = 100
    ) -> dict:
        """One page of ``/transactions/sync``: added/modified/removed + next_cursor."""
        payload: dict[str, Any] = {"access_token": access_token, "count": count}
        if cursor:
            payload["cursor"] = cursor
        return self._post("/transactions/sync", payload)

    def sync_all_transactions(
        self,
        access_token: str,
        cursor: str | None = None,
        *,
        max_wait_seconds: float = 90.0,
    ) -> tuple[list[dict], list[dict], str | None]:
        """Drain the sync cursor. Returns ``(added, removed, next_cursor)``.

        A freshly linked item reports ``PRODUCT_NOT_READY`` (or an empty first
        sync with ``has_more=false``) while Plaid pulls history, so this retries
        with backoff up to ``max_wait_seconds`` — needed for the sandbox demo,
        harmless for an established item. ``modified`` rows are folded into
        ``added``: the load is idempotent on ``transaction_id``.
        """
        deadline = time.monotonic() + max_wait_seconds
        added: list[dict] = []
        removed: list[dict] = []
        while True:
            try:
                page = self.sync_transactions(access_token, cursor)
            except PlaidError as exc:
                if exc.error_code == "PRODUCT_NOT_READY" and time.monotonic() < deadline:
                    time.sleep(3)
                    continue
                raise
            added.extend(page["added"])
            added.extend(page["modified"])
            removed.extend(page["removed"])
            cursor = page["next_cursor"]
            if page["has_more"]:
                continue
            # First sync of a new item can legitimately come back empty while
            # history is still being pulled; give it a moment and re-poll.
            if not added and not removed and time.monotonic() < deadline:
                time.sleep(3)
                continue
            return added, removed, cursor
