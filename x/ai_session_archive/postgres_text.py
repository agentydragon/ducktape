"""Validation helpers for values projected into PostgreSQL text columns."""


def is_safe_postgres_text(value: str) -> bool:
    """Return whether PostgreSQL can store the value as UTF-8 text."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True
