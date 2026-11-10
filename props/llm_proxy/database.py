"""FastAPI dependency for the standalone LLM proxy's async database."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from props.db.async_database import AsyncDatabase


def get_proxy_db(request: Request) -> AsyncDatabase:
    return request.app.state.admin_db  # type: ignore[no-any-return]


ProxyDb = Annotated[AsyncDatabase, Depends(get_proxy_db)]
