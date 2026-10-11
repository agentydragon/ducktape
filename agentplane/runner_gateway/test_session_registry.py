"""The Session registry in the gateway's own migrated database."""

import asyncio
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner_gateway.session_registry import SessionConflictError, SessionNotFoundError, SessionRegistry


async def test_a_session_registers_in_one_sandbox_incarnation(sessions: SessionRegistry, engine: AsyncEngine) -> None:
    replica = SessionRegistry(engine)
    uid = uuid4()
    session_id = uuid4()
    where = {"sandbox_namespace": "testing", "sandbox_name": "worker"}

    # Concurrent retries on two replicas both find it registered.
    await asyncio.gather(
        sessions.register(session_id, **where, sandbox_uid=uid), replica.register(session_id, **where, sandbox_uid=uid)
    )
    with pytest.raises(SessionConflictError):
        await replica.register(session_id, **where, sandbox_uid=uuid4())
    # The client's ID is the runner's Session ID too, registered in this incarnation only.
    assert await replica.public_id(str(session_id), **where, sandbox_uid=uid) == session_id
    assert await replica.public_id(str(session_id), **where, sandbox_uid=uuid4()) is None
    assert await replica.public_id("not-a-uuid", **where, sandbox_uid=uid) is None
    with pytest.raises(SessionNotFoundError):
        await sessions.require(session_id, **where, sandbox_uid=uuid4())


if __name__ == "__main__":
    pytest_bazel.main()
