"""PostgreSQL URL conversions for libraries that need a rendered DSN."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url


def render_postgresql_url(url: str | URL, drivername: str) -> str:
    """Render a PostgreSQL URL for a client driver."""
    return make_url(url).set(drivername=drivername).render_as_string(hide_password=False)
