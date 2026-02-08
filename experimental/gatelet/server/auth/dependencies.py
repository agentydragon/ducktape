"""Authentication dependencies for FastAPI routes."""

from typing import Annotated

from fastapi import Cookie, Depends

from experimental.gatelet.server import database
from experimental.gatelet.server.auth.handlers import AuthContext, admin_auth, key_path_auth, session_auth
from experimental.gatelet.server.config import Settings, get_settings


class AuthDependency:
    """Provide the active authentication context."""

    def __init__(self, initial_context: AuthContext | None = None):
        self.context: AuthContext | None = initial_context

    def set_context(self, context: AuthContext) -> None:
        self.context = context

    async def __call__(self) -> AuthContext:
        if self.context is None:
            raise RuntimeError("Auth context not initialized")
        return self.context


auth_dependency = AuthDependency()
Auth = Annotated[AuthContext, Depends(auth_dependency)]


async def get_key_path_auth_with_context(key: str, settings: Settings = Depends(get_settings)) -> AuthContext:
    async with database.get_db_session() as session:
        auth_context = await key_path_auth(key, session, settings)
        auth_dependency.set_context(auth_context)
        return auth_context


async def get_session_auth_with_context(session_token: str, settings: Settings = Depends(get_settings)) -> AuthContext:
    async with database.get_db_session() as session:
        auth_context = await session_auth(session_token, session, settings)
        auth_dependency.set_context(auth_context)
        return auth_context


async def get_admin_auth_with_context(session_token: str | None = Cookie(None)) -> AuthContext:
    if session_token is None:
        raise RuntimeError("No admin session")
    async with database.get_db_session() as session:
        auth_context = await admin_auth(session_token, session)
        auth_dependency.set_context(auth_context)
        return auth_context
