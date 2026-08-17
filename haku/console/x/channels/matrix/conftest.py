"""Shared setup for the Matrix channel's tests: who Haku and the operator are on a homeserver,
which room they are in, and the room/session binding.

Everything neutral — the stores, the service, the claim stand-in, the operator's identity — comes
from the runtime's own <../../conftest.py>, which is deliberately free of anything a homeserver
knows about.

`OPERATOR_SUBJECT` is imported rather than restated because both levels have to agree on it:
`MATRIX_CONFIG.operator_subject` is what ingress resolves a sender through, and the `operator_id`
fixture is what the runtime's stores were told owns the session. Two literals here would let a test
bind a room to an operator no session belongs to.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.config import MatrixConfig
from haku.console.x.channels.matrix.session import MatrixConversationStore
from haku.console.x.conftest import OPERATOR_SUBJECT

MATRIX_USER = "@haku:allegedly.works"
MATRIX_OPERATOR = "@rai:allegedly.works"
MATRIX_ROOM = "!room:allegedly.works"

MATRIX_CONFIG = MatrixConfig(
    homeserver="https://matrix.allegedly.works",
    user_id=MATRIX_USER,
    operator_user_id=MATRIX_OPERATOR,
    operator_subject=OPERATOR_SUBJECT,
)


@pytest.fixture
def conversations(migrated_sessions: async_sessionmaker[AsyncSession]) -> MatrixConversationStore:
    return MatrixConversationStore(migrated_sessions)
