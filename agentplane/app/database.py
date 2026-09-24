"""The app's PostgreSQL database: the declarative base every table maps onto, and one connection pool
shared by every store and the update listener."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# SQLAlchemy loads the asyncpg dialect from the URL scheme; nothing imports it directly.
# gazelle:include_dep @pypi//asyncpg


class Base(DeclarativeBase):
    pass


def connect(database_url: str) -> AsyncEngine:
    # Lazy: no connection is made until a store first uses one.
    return create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
