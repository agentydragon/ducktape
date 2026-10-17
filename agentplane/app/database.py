"""The app's PostgreSQL database: the declarative base every table maps onto, and one connection pool
shared by every store and the update listener."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# SQLAlchemy loads asyncpg dynamically from the URL; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


class Base(DeclarativeBase):
    pass


def connect(database_url: str) -> AsyncEngine:
    # Lazy: no connection is made until a store first uses one.
    # Staging OOM: 74 sessions at 512 MiB, 67 idle.
    url = make_url(database_url).set(drivername="postgresql+asyncpg")
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True, pool_size=4, max_overflow=2)
