"""Database session management for Gatelet server."""

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession]:
    """Get a database session (FastAPI dependency).

    Use as: db: AsyncSession = Depends(get_db_session)

    The session factory is managed by the application lifespan and stored on app.state.
    Sessions are created per-request and automatically committed/rolled back.
    """
    factory = request.app.state.db_session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
