"""Small helpers for the as_of date: the "today" the research is done as of."""

from __future__ import annotations

from datetime import date
from typing import Optional


def check_date(value: Optional[str], name: str) -> Optional[str]:
    """Return `value` unchanged if it is None or a valid YYYY-MM-DD date."""
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a date in YYYY-MM-DD form, got {value!r}") from None
    if len(value) != 10:
        raise ValueError(f"{name} must be a date in YYYY-MM-DD form, got {value!r}")
    return value


def on_or_before(day: str, as_of: Optional[str]) -> bool:
    """True if `day` is on or before `as_of` (or if there is no as_of)."""
    return as_of is None or (bool(day) and day <= as_of)
