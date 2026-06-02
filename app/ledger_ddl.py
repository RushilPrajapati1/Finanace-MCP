"""Raw DDL that the ORM cannot express: append-only enforcement.

A real ledger is immutable. Once a transaction is posted it is never edited or
deleted; corrections happen by posting a *reversing* transaction. We enforce
this at the database level with triggers so that no application bug — and no
ad-hoc ``UPDATE`` at a psql prompt — can rewrite financial history.

These statements are applied by the Alembic migration (production) and by the
test bootstrap (so tests exercise the same guarantees).
"""

from __future__ import annotations

IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION finledger_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'relation % is append-only; % is rejected to preserve ledger immutability',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;
"""

_IMMUTABLE_TABLES = ("transactions", "postings")

CREATE_TRIGGERS = [
    f"""
    CREATE TRIGGER trg_{table}_immutable
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION finledger_block_mutation();
    """
    for table in _IMMUTABLE_TABLES
]

DROP_TRIGGERS = [
    f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table};"
    for table in _IMMUTABLE_TABLES
]

DROP_FUNCTION = "DROP FUNCTION IF EXISTS finledger_block_mutation();"


# Seed data for the most common fiat and crypto currencies. (code, exponent, name)
DEFAULT_CURRENCIES: list[tuple[str, int, str]] = [
    ("USD", 2, "US Dollar"),
    ("EUR", 2, "Euro"),
    ("GBP", 2, "Pound Sterling"),
    ("JPY", 0, "Japanese Yen"),
    ("INR", 2, "Indian Rupee"),
    ("CHF", 2, "Swiss Franc"),
    ("CAD", 2, "Canadian Dollar"),
    ("AUD", 2, "Australian Dollar"),
    ("SGD", 2, "Singapore Dollar"),
    ("BTC", 8, "Bitcoin"),
    ("ETH", 18, "Ether"),
    ("USDC", 6, "USD Coin"),
]


def apply_immutability_sql() -> list[str]:
    """Ordered statements to (re)install the append-only triggers idempotently."""
    return [*DROP_TRIGGERS, IMMUTABILITY_FUNCTION, *CREATE_TRIGGERS]
