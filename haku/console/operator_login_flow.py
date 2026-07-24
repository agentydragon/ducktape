"""Server-side state for pending operator browser logins.

authlib keeps a pending authorization request in the Starlette session cookie, and its Starlette
integration deliberately clears every prior ``_state_<name>_*`` entry each time it stores a new one
("clear old state data to avoid session size growing"). One browser therefore holds exactly one
pending login: as soon as a second console tab starts one, the first tab's callback fails with
``mismatching_state``. That is routine here — the operator session has an absolute one-hour
deadline, so every open tab bounces to ``/auth/login`` at about the same time.

So the console keeps each attempt in its own ``operator_login_flows`` row instead, the same shape
the account-link flows already use (`mcp_operator_oauth.py`, `provider_connection.py`). Concurrent
attempts no longer interact. The user-agent binding RFC 6749 §10.12 wants is preserved explicitly:
each flow mints a secret, hands it to the browser in a cookie **named after that flow's state**, and
the callback refuses a flow whose secret the browser cannot produce. Per-flow cookie names are the
point — one shared cookie would re-create exactly the eviction this table exists to avoid, since
concurrent tabs would each overwrite the last one's value.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from authlib.integrations.starlette_client import OAuth, StarletteIntegration
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import OperatorLoginFlow

logger = logging.getLogger(__name__)

LOGIN_COOKIE_PATH = "/auth"
_BINDING_COOKIE_PREFIX = "haku_console_login_"
FLOW_LIFETIME_SECONDS = 15 * 60


def binding_cookie_name(state: str) -> str:
    """One cookie per pending login, so concurrent attempts never overwrite each other's binding."""
    return f"{_BINDING_COOKIE_PREFIX}{state}"


def new_browser_binding() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """The parts of a flow the callback needs before authlib consumes the row."""

    browser_binding: str
    return_to: str | None

    def started_by(self, presented_binding: str | None) -> bool:
        return presented_binding is not None and secrets.compare_digest(self.browser_binding, presented_binding)


class PostgresOperatorLoginFlowStore:
    """The pending-login rows, plus the authlib-facing state accessors."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def start(self, *, state: str, browser_binding: str, return_to: str | None, data: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.UTC)
        with self._sessions.begin() as session:
            session.execute(delete(OperatorLoginFlow).where(OperatorLoginFlow.expires_at < now))
            session.add(
                OperatorLoginFlow(
                    state=state,
                    browser_binding=browser_binding,
                    return_to=return_to,
                    data=data,
                    created_at=now,
                    expires_at=now + datetime.timedelta(seconds=FLOW_LIFETIME_SECONDS),
                )
            )

    def pending_login(self, state: str) -> PendingLogin | None:
        """Read a live flow without consuming it — authlib's own exchange deletes the row."""
        with self._sessions.begin() as session:
            row = self._live_row(session, state)
            return None if row is None else PendingLogin(browser_binding=row.browser_binding, return_to=row.return_to)

    def state_data(self, state: str) -> dict[str, Any] | None:
        with self._sessions.begin() as session:
            row = self._live_row(session, state)
            return None if row is None else dict(row.data)

    def store_state_data(self, state: str, data: dict[str, Any]) -> None:
        """authlib re-states the flow's data after building the authorization URL. The row already
        exists (``start`` wrote it with the browser binding), so this only refreshes the payload."""
        with self._sessions.begin() as session:
            row = session.get(OperatorLoginFlow, state)
            if row is None:
                logger.warning("operator login: no pending flow to store authorization data for")
                return
            row.data = data

    def discard(self, state: str) -> None:
        with self._sessions.begin() as session:
            session.execute(delete(OperatorLoginFlow).where(OperatorLoginFlow.state == state))

    @staticmethod
    def _live_row(session: Session, state: str) -> OperatorLoginFlow | None:
        return session.execute(
            select(OperatorLoginFlow)
            .where(OperatorLoginFlow.state == state)
            .where(OperatorLoginFlow.expires_at > datetime.datetime.now(datetime.UTC))
        ).scalar_one_or_none()


class PostgresLoginStateIntegration(StarletteIntegration):
    """authlib framework integration whose pending-login state is a Postgres row, not a session key.

    The store arrives through authlib's ``cache`` slot because that is the only value
    ``BaseOAuth.create_client`` threads into a framework integration. The ``session`` argument is
    ignored on purpose: binding lives on the flow row (see the module docstring), so nothing about
    an in-flight login touches the shared cookie.
    """

    @property
    def _store(self) -> PostgresOperatorLoginFlowStore:
        store = self.cache
        assert isinstance(store, PostgresOperatorLoginFlowStore)
        return store

    async def get_state_data(self, session: dict[str, Any] | None, state: str) -> dict[str, Any]:
        # An unknown state has no data. authlib's own implementation returns None here too — its
        # type stub just does not say so — and `authorize_access_token` turns that into the
        # MismatchingStateError the callback reports.
        data = await asyncio.to_thread(self._store.state_data, state) if state else None
        return cast(dict[str, Any], data)

    async def set_state_data(self, session: dict[str, Any] | None, state: str, data: Any) -> None:
        await asyncio.to_thread(self._store.store_state_data, state, data)

    async def clear_state_data(self, session: dict[str, Any] | None, state: str) -> None:
        await asyncio.to_thread(self._store.discard, state)


class LoginFlowOAuth(OAuth):
    framework_integration_cls = PostgresLoginStateIntegration
