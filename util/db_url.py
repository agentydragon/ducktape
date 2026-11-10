"""PostgreSQL URL conversions for libraries that need a rendered DSN."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url


def render_postgresql_url(
    url: str | URL, drivername: str, *, query: Mapping[str, str | tuple[str, ...]] | None = None
) -> str:
    """Render a PostgreSQL URL for a client driver, optionally replacing its query parameters."""
    parsed = make_url(url)
    if query is not None:
        parsed = parsed.set(query=query)
    return parsed.set(drivername=drivername).render_as_string(hide_password=False)


def async_engine_url(url: str | URL) -> URL:
    """Use asyncpg for plain PostgreSQL URLs, preserving explicitly selected drivers."""
    parsed = make_url(url)
    return parsed.set(drivername="postgresql+asyncpg") if parsed.drivername == "postgresql" else parsed
