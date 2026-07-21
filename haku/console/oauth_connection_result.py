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
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import OAuthConnectionResult as OAuthConnectionResultRow
from haku.console.operator_auth import OperatorActorDep
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

OAUTH_RESULT_PATH_PREFIX = "/_console/oauth-result"
OAUTH_RESULT_SETTINGS_PATH = "/_console/settings"
_RESULT_TTL = datetime.timedelta(minutes=5)


class OAuthConnectionSucceeded(BaseModel):
    status: Literal["success"] = "success"
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


class OAuthConnectionFailed(BaseModel):
    status: Literal["error"] = "error"
    title: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1000)


OAuthConnectionResult = Annotated[OAuthConnectionSucceeded | OAuthConnectionFailed, Field(discriminator="status")]


def bounded_result_message(message: str, *, fallback: str) -> str:
    normalized = message.strip() or fallback
    return normalized[:1000]


class PostgresOAuthConnectionResultStore:
    def __init__(
        self, sessions: sessionmaker[Session], *, operator_identity_store: PostgresOperatorIdentityStore
    ) -> None:
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store

    def create(self, *, operator_id: UUID, result: OAuthConnectionResult) -> UUID:
        now = datetime.datetime.now(datetime.UTC)
        result_id = uuid4()
        with self._sessions() as session, session.begin():
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            session.execute(delete(OAuthConnectionResultRow).where(OAuthConnectionResultRow.expires_at <= now))
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

    def consume(self, *, result_id: UUID, operator_id: UUID) -> OAuthConnectionResult | None:
        now = datetime.datetime.now(datetime.UTC)
        with self._sessions() as session, session.begin():
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            session.execute(delete(OAuthConnectionResultRow).where(OAuthConnectionResultRow.expires_at <= now))
            row = session.scalar(
                select(OAuthConnectionResultRow)
                .where(
                    OAuthConnectionResultRow.result_id == result_id, OAuthConnectionResultRow.operator_id == operator_id
                )
                .with_for_update()
            )
            if row is None:
                return None
            session.delete(row)
            if row.status == "success":
                return OAuthConnectionSucceeded(title=row.title, message=row.message)
            if row.status == "error":
                return OAuthConnectionFailed(title=row.title, message=row.message)
            raise AssertionError(f"database accepted unknown OAuth connection result status {row.status!r}")


def _store(request: Request) -> PostgresOAuthConnectionResultStore:
    return cast(PostgresOAuthConnectionResultStore, request.app.state.oauth_connection_result_store)


OAuthConnectionResultStoreDep = Annotated[PostgresOAuthConnectionResultStore, Depends(_store)]
router = APIRouter(tags=["oauth-connection-results"])


def result_redirect(
    store: PostgresOAuthConnectionResultStore,
    *,
    operator_id: UUID,
    result: OAuthConnectionResult,
    destination: Literal["result", "settings"] = "result",
) -> RedirectResponse:
    result_id = store.create(operator_id=operator_id, result=result)
    url = (
        f"{OAUTH_RESULT_SETTINGS_PATH}?{urlencode({'oauth_result': str(result_id)})}"
        if destination == "settings"
        else f"{OAUTH_RESULT_PATH_PREFIX}/{result_id}"
    )
    return RedirectResponse(url=url, status_code=303)


@router.post("/api/oauth-results/{result_id}", response_model=OAuthConnectionResult)
def consume_oauth_connection_result(
    result_id: UUID, store: OAuthConnectionResultStoreDep, actor: OperatorActorDep
) -> OAuthConnectionResult:
    result = store.consume(result_id=result_id, operator_id=actor.operator_id)
    if result is None:
        raise HTTPException(status_code=404, detail="OAuth connection result expired or was already viewed")
    return result
