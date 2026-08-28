"""One-time handoff from account-link callbacks to the trusted console SPA."""

from __future__ import annotations

import datetime
from typing import Annotated, Literal, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import OAuthConnectionResultRow
from haku.console.operator_auth import OperatorActorDep
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

OAUTH_RESULT_PATH_PREFIX = "/_console/oauth-result"
OAUTH_RESULT_SETTINGS_PATH = "/_console/settings"
_RESULT_TTL = datetime.timedelta(minutes=5)


class ConnectionSucceeded(BaseModel):
    status: Literal["success"] = "success"
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


class ConnectionFailed(BaseModel):
    status: Literal["error"] = "error"
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


ConnectionResult = Annotated[ConnectionSucceeded | ConnectionFailed, Field(discriminator="status")]


def bounded_result_message(message: str, *, fallback: str) -> str:
    normalized = message.strip() or fallback
    return normalized[:1000]


class PostgresConnectionResultStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, operator_identity_store: PostgresOperatorIdentityStore
    ) -> None:
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store

    async def create(self, *, operator_id: UUID, result: ConnectionResult) -> UUID:
        now = datetime.datetime.now(datetime.UTC)
        result_id = uuid4()
        async with self._sessions() as session, session.begin():
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            await session.execute(delete(OAuthConnectionResultRow).where(OAuthConnectionResultRow.expires_at <= now))
            session.add(
                OAuthConnectionResultRow(
                    result_id=result_id,
                    operator_id=operator_id,
                    status=result.status,
                    title=result.title,
                    message=result.message,
                    created_at=now,
                    expires_at=now + _RESULT_TTL,
                )
            )
        return result_id

    async def consume(self, *, result_id: UUID, operator_id: UUID) -> ConnectionResult | None:
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as session, session.begin():
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            await session.execute(delete(OAuthConnectionResultRow).where(OAuthConnectionResultRow.expires_at <= now))
            row = await session.scalar(
                select(OAuthConnectionResultRow)
                .where(
                    OAuthConnectionResultRow.result_id == result_id, OAuthConnectionResultRow.operator_id == operator_id
                )
                .with_for_update()
            )
            if row is None:
                return None
            await session.delete(row)
            if row.status == "success":
                return ConnectionSucceeded(title=row.title, message=row.message)
            if row.status == "error":
                return ConnectionFailed(title=row.title, message=row.message)
            raise AssertionError(f"database accepted unknown OAuth connection result status {row.status!r}")


def _store(request: Request) -> PostgresConnectionResultStore:
    return cast(PostgresConnectionResultStore, request.app.state.oauth_connection_result_store)


ConnectionResultStoreDep = Annotated[PostgresConnectionResultStore, Depends(_store)]
router = APIRouter(tags=["oauth-connection-results"])


async def result_redirect(
    store: PostgresConnectionResultStore,
    *,
    operator_id: UUID,
    result: ConnectionResult,
    destination: Literal["result", "settings"] = "result",
) -> RedirectResponse:
    result_id = await store.create(operator_id=operator_id, result=result)
    url = (
        f"{OAUTH_RESULT_SETTINGS_PATH}?{urlencode({'oauth_result': str(result_id)})}"
        if destination == "settings"
        else f"{OAUTH_RESULT_PATH_PREFIX}/{result_id}"
    )
    return RedirectResponse(url=url, status_code=303)


@router.post("/api/oauth-results/{result_id}", response_model=ConnectionResult)
async def consume_connection_result(
    result_id: UUID, store: ConnectionResultStoreDep, actor: OperatorActorDep
) -> ConnectionResult:
    result = await store.consume(result_id=result_id, operator_id=actor.operator_id)
    if result is None:
        raise HTTPException(status_code=404, detail="OAuth connection result expired or was already viewed")
    return result
