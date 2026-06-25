"""ISO date/timestamp parsing for search and reporting date ranges.

One convention, shared by :mod:`app.services.reporting` and
:func:`app.services.ledger.search_transactions`, so every date filter in the
ledger behaves identically:

* Ranges are **half-open**: ``[start, end)``. ``start`` is inclusive.
* A **date-only** bound (``YYYY-MM-DD``) means the whole calendar day in UTC.
  As an *exclusive end* it therefore advances to the next day's midnight, so the
  named day is fully included. As an *inclusive "as of"* it covers through the
  last microsecond of that day.
* A **full timestamp** is used exactly as given (assumed UTC if it carries no
  offset), so callers who need sub-day precision get it.

All returned datetimes are timezone-aware UTC to match the ``timestamptz``
columns they are compared against.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from app.domain.errors import ValidationError

_DAY = timedelta(days=1)
# The last representable instant within a calendar day (microsecond precision,
# matching Postgres ``timestamptz``).
_END_OF_DAY = time(23, 59, 59, 999999)


def _parse(value: str, field: str) -> tuple[datetime, bool]:
    """Parse an ISO date or timestamp into a UTC-aware datetime.

    Returns ``(dt, date_only)`` where ``date_only`` is ``True`` when ``value``
    carried no time component. Raises :class:`ValidationError` on malformed
    input so the MCP layer surfaces a structured error instead of a 500.
    """
    text = value.strip()
    # Try a bare calendar date first; ``date.fromisoformat`` rejects anything
    # with a time component, which is exactly how we tell the two apart.
    try:
        d = date.fromisoformat(text)
    except ValueError:
        pass
    else:
        return datetime.combine(d, time.min, tzinfo=UTC), True

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"invalid {field}: {value!r} (use an ISO date or timestamp)"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC), False


def parse_start(value: str | None) -> datetime | None:
    """Inclusive lower bound for a range, or ``None`` for no lower bound."""
    if value is None:
        return None
    dt, _ = _parse(value, "start_date")
    return dt


def parse_end_exclusive(value: str | None) -> datetime | None:
    """Exclusive upper bound for a range, or ``None`` for no upper bound.

    A date-only ``value`` advances to the following midnight so the named day is
    included in full; a full timestamp is used as the exact (exclusive) cutoff.
    """
    if value is None:
        return None
    dt, date_only = _parse(value, "end_date")
    return dt + _DAY if date_only else dt


def parse_as_of_inclusive(value: str) -> datetime:
    """Inclusive instant for a point-in-time ("as of") snapshot.

    A date-only ``value`` resolves to the last microsecond of that day, so a
    balance sheet "as of 2026-06-30" includes everything posted on the 30th.
    Reporting compares with ``<=`` against this instant.
    """
    dt, date_only = _parse(value, "as_of_date")
    return datetime.combine(dt.date(), _END_OF_DAY, tzinfo=UTC) if date_only else dt
