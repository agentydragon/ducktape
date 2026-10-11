"""The Session registry in the Sandbox Service's own migrated database."""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.runner_gateway.session_registry import (
    OpenConflictError,
    OpenReservation,
    SessionNotFoundError,
    SessionRegistry,
)
from agentplane.runner_gateway.testing.sessions import register_session

# gazelle:include_dep //agentplane/runner_gateway/commands:conftest

pytest_plugins = ("agentplane.runner_gateway.commands.conftest",)


@pytest.fixture
def registry(engine: AsyncEngine) -> SessionRegistry:
    return SessionRegistry(engine)


async def test_reservation_is_stable_across_retries_and_replicas(
    registry: SessionRegistry, engine: AsyncEngine
) -> None:
    replica = SessionRegistry(engine)
    uid = uuid4()

    async def reserve(
        on: SessionRegistry,
        *,
        key: str = "open-1",
        caller_name: str = "app",
        sandbox_uid: UUID = uid,
        payload: bytes = b"spec",
        launch: str = "frozen",
    ) -> OpenReservation:
        return await on.reserve(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
            open_request=payload,
            launch_spec=lambda candidate: f"{launch} {candidate}".encode(),
        )

    first, second = await asyncio.gather(reserve(registry), reserve(replica))
    assert first == second
    assert first.launch_spec == f"frozen {first.session_id}".encode()
    assert await reserve(replica, launch="changed default should be ignored") == first
    assert (
        await registry.runner_id(first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid)
        == f"r-{first.session_id}"
    )
    assert (
        await replica.session_id(
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=uid,
            runner_session_id=f"r-{first.session_id}",
        )
        == first.session_id
    )
    with pytest.raises(SessionNotFoundError):
        await registry.runner_id(
            first.session_id, sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uuid4()
        )
    assert await reserve(registry, key="open-2") != first
    assert await reserve(registry, caller_name="other") != first
    assert await reserve(registry, sandbox_uid=uuid4()) != first
    with pytest.raises(OpenConflictError, match="different inputs"):
        await reserve(registry, payload=b"different caller request")
    assert await reserve(replica) == first  # a bad retry does not change the reservation
    with pytest.raises(ValueError, match="required"):
        await reserve(registry, key="")

    async def lookup(*, caller_name: str = "app", sandbox_uid: UUID = uid, key: str = "open-1") -> UUID | None:
        return await replica.lookup_open(
            caller_namespace="testing",
            caller_name=caller_name,
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=sandbox_uid,
            open_key=key,
        )

    assert await lookup() == first.session_id
    assert await lookup(caller_name="not-this-caller") is None
    assert await lookup(sandbox_uid=uuid4()) is None
    assert await lookup(key="absent") is None


async def test_a_runner_locator_belongs_to_one_session(engine: AsyncEngine) -> None:
    uid = uuid4()
    await register_session(
        engine, uuid4(), sandbox_namespace="testing", sandbox_name="worker", sandbox_uid=uid, runner_session_id="s-1"
    )
    with pytest.raises(IntegrityError):
        await register_session(
            engine,
            uuid4(),
            sandbox_namespace="testing",
            sandbox_name="worker",
            sandbox_uid=uid,
            runner_session_id="s-1",
        )
    # A nullable legacy UID must not permit another ID for the same legacy locator.
    await register_session(
        engine, uuid4(), sandbox_namespace="testing", sandbox_name="gone", sandbox_uid=None, runner_session_id="legacy"
    )
    with pytest.raises(IntegrityError):
        await register_session(
            engine,
            uuid4(),
            sandbox_namespace="testing",
            sandbox_name="gone",
            sandbox_uid=None,
            runner_session_id="legacy",
        )


if __name__ == "__main__":
    pytest_bazel.main()
