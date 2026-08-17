"""Server-side state for pending operator browser logins.

authlib keeps a pending authorization request in the Starlette session cookie, and its Starlette
integration clears every prior ``_state_<name>_*`` entry each time it stores a new one. One browser
therefore holds exactly one pending login: as soon as a second console tab starts one, the first
tab's callback fails with ``mismatching_state`` — routine here, since the operator session has an
absolute one-hour deadline and every open tab bounces to ``/auth/login`` at about the same time.

The console keeps each attempt in its own ``operator_login_flows`` row instead, the same shape the
account-link flows already use (`mcp_operator_oauth.py`, `provider_connection.py`), so concurrent
attempts do not interact. The user-agent binding RFC 6749 §10.12 wants is preserved explicitly:
each flow mints a secret, hands it to the browser in a cookie **named after that flow's state**, and
the callback refuses a flow whose secret the browser cannot produce. Per-flow cookie names are the
point — one shared cookie would re-create the eviction this table exists to avoid.
"""

from __future__ import annotations

import datetime
import logging
import secrets
from dataclasses import dataclass
from typing import Any, cast

from authlib.integrations.starlette_client import OAuth, StarletteIntegration
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def start(self, *, state: str, browser_binding: str, return_to: str | None, data: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions.begin() as session:
            await session.execute(delete(OperatorLoginFlow).where(OperatorLoginFlow.expires_at < now))
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

    async def pending_login(self, state: str) -> PendingLogin | None:
        """Read a live flow without consuming it — authlib's own exchange deletes the row."""
        async with self._sessions.begin() as session:
            row = await self._live_row(session, state)
            return None if row is None else PendingLogin(browser_binding=row.browser_binding, return_to=row.return_to)

    async def state_data(self, state: str) -> dict[str, Any] | None:
        async with self._sessions.begin() as session:
            row = await self._live_row(session, state)
            return None if row is None else dict(row.data)

    async def store_state_data(self, state: str, data: dict[str, Any]) -> None:
        """authlib re-states the flow's data after building the authorization URL. The row already
        exists (``start`` wrote it with the browser binding), so this only refreshes the payload."""
        async with self._sessions.begin() as session:
            row = await session.get(OperatorLoginFlow, state)
            if row is None:
                logger.warning("operator login: no pending flow to store authorization data for")
                return
            row.data = data

    async def discard(self, state: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(delete(OperatorLoginFlow).where(OperatorLoginFlow.state == state))

    @staticmethod
    async def _live_row(session: AsyncSession, state: str) -> OperatorLoginFlow | None:
        return (
            await session.execute(
                select(OperatorLoginFlow)
                .where(OperatorLoginFlow.state == state)
                .where(OperatorLoginFlow.expires_at > datetime.datetime.now(datetime.UTC))
            )
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
        data = await self._store.state_data(state) if state else None
        return cast(dict[str, Any], data)

    async def set_state_data(self, session: dict[str, Any] | None, state: str, data: Any) -> None:
        await self._store.store_state_data(state, data)

    async def clear_state_data(self, session: dict[str, Any] | None, state: str) -> None:
        await self._store.discard(state)


class LoginFlowOAuth(OAuth):
    framework_integration_cls = PostgresLoginStateIntegration
