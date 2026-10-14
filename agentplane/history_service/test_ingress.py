"""The runner ingress over a migrated history database, driven as a runner drives it."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.grpc_serving import serving
from agentplane.history_service.ingress import Resources, Timing, add_ingress
from agentplane.history_service.storage.store import Store
from agentplane.proto import event_log_pb2, event_pb2, runner_ingress_pb2, runner_ingress_pb2_grpc, runner_pb2
from agentplane.workload_auth.principal import POD_NAME_CLAIM, POD_UID_CLAIM, WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf

AUDIENCE = "test-runner-ingress"
NAMESPACE = "test-sandbox-namespace"
# Bearer -> (ServiceAccount name, ServiceAccount UID). Test literals, not real credentials.
ACCOUNTS = {
    "test-token-sandbox-a": ("test-sandbox-a", "test-account-a-uid"),
    "test-token-sandbox-b": ("test-sandbox-b", "test-account-b-uid"),
    # Sandbox A's account deleted and recreated under the same name.
    "test-token-sandbox-a-recreated": ("test-sandbox-a", "test-account-a-recreated-uid"),
}
WRITER = "test-token-sandbox-a"


class Authentication:
    async def create_token_review(self, body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        account = ACCOUNTS.get(body.spec.token)
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=account is not None,
                audiences=[AUDIENCE],
                user=k8s_client.V1UserInfo(
                    username=f"system:serviceaccount:{NAMESPACE}:{account[0]}",
                    uid=account[1],
                    extra={POD_NAME_CLAIM: ["test-sandbox-pod"], POD_UID_CLAIM: ["test-sandbox-pod-uid"]},
                )
                if account is not None
                else None,
            ),
        )


def entry(cursor: int, text: str = "journal") -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-runner-journal", sequence=cursor),
        event=event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=f"{text} {cursor}")),
    )


def announce(
    session_id: UUID, last_cursor: int = 0, *, incarnation_uid: str | None = None
) -> runner_ingress_pb2.PushFrame:
    return runner_ingress_pb2.PushFrame(
        announce=runner_pb2.Attached(
            session_id=str(session_id), last_cursor=last_cursor, incarnation_uid=incarnation_uid
        )
    )


def entries(*cursors: int, text: str = "journal") -> list[runner_ingress_pb2.PushFrame]:
    return [runner_ingress_pb2.PushFrame(entry=entry(cursor, text)) for cursor in cursors]


def sealed(cursor: int, *, incarnation_uid: str | None = None) -> runner_ingress_pb2.PushFrame:
    return runner_ingress_pb2.PushFrame(sealed=runner_pb2.Sealed(cursor=cursor, incarnation_uid=incarnation_uid))


@asynccontextmanager
async def ingress(engine: AsyncEngine) -> AsyncIterator[runner_ingress_pb2_grpc.RunnerIngressAsyncStub]:
    server = grpc.aio.server()
    add_ingress(
        Resources(
            principals=WorkloadPrincipalResolver(
                authentication=cast(k8s_client.AuthenticationV1Api, Authentication()),
                audience=AUDIENCE,
                allowed_service_account_namespaces={NAMESPACE},
            ),
            history=Store(engine),
            timing=Timing(idle_ack_s=0.2, silence_limit_s=0.6),
        ),
        server,
    )
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel,
    ):
        yield runner_ingress_pb2_grpc.RunnerIngressStub(channel)


class Call:
    """A runner's Push call: frames go out as the test sends them; acknowledgements are read back."""

    def __init__(self, stub: runner_ingress_pb2_grpc.RunnerIngressAsyncStub, token: str) -> None:
        self.call: grpc.aio.StreamStreamCall[runner_ingress_pb2.PushFrame, runner_ingress_pb2.Acknowledgement] = (
            stub.Push(metadata=(("authorization", f"Bearer {token}"),))
        )

    async def send(self, *frames: runner_ingress_pb2.PushFrame) -> None:
        for frame in frames:
            await self.call.write(frame)

    async def acknowledged(self, cursor: int, *, sealed: bool = False) -> None:
        """Read acknowledgements until one reaches `cursor`; none may decrease."""
        previous = 0
        async with asyncio.timeout(10):
            while True:
                acknowledgement = await self.call.read()
                assert isinstance(acknowledgement, runner_ingress_pb2.Acknowledgement), "the ingress ended the call"
                assert acknowledgement.cursor >= previous
                previous = acknowledgement.cursor
                if acknowledgement.cursor >= cursor and acknowledgement.sealed == sealed:
                    assert acknowledgement.cursor == cursor
                    return

    async def refused(self) -> grpc.StatusCode:
        async with asyncio.timeout(10):
            with pytest.raises(grpc.aio.AioRpcError) as raised:
                await self._drain()
        return raised.value.code()

    async def _drain(self) -> None:
        while await self.call.read() is not grpc.aio.EOF:
            pass


async def push(
    stub: runner_ingress_pb2_grpc.RunnerIngressAsyncStub, frames: Sequence[runner_ingress_pb2.PushFrame]
) -> Call:
    call = Call(stub, WRITER)
    await call.send(*frames)
    return call


async def committed(engine: AsyncEngine, session_id: UUID) -> list[str]:
    stored = list((await Store(engine).read_page(session_id, limit=100)).entries)
    return [stored_entry.event.harness_stderr.text for stored_entry in stored]


async def test_entries_are_acknowledged_after_commit_and_the_seal_ends_the_journal(engine: AsyncEngine) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(stub, [announce(session_id, last_cursor=2)])
        await call.acknowledged(0)
        await call.send(*entries(1, 2))
        await call.acknowledged(2)
        assert await committed(engine, session_id) == ["journal 1", "journal 2"]
        await call.send(*entries(3), sealed(3))
        await call.acknowledged(3, sealed=True)
        assert await call.call.read() is grpc.aio.EOF
    page = await Store(engine).read_page(session_id, after_cursor=0, limit=10)
    assert (page.feed_state.attached.last_cursor, page.feed_state.sealed.cursor) == (2, 3)


async def test_resume_replays_exact_duplicates_and_continues_after_a_restart(engine: AsyncEngine) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(stub, [announce(session_id), *entries(1, 2)])
        await call.acknowledged(2)
        call.call.cancel()
    # A new ingress process, and a runner that recorded only the first acknowledgement.
    async with ingress(engine) as stub:
        call = await push(stub, [announce(session_id, last_cursor=3)])
        await call.acknowledged(2)
        await call.send(*entries(1, 2, 3), sealed(3))
        await call.acknowledged(3, sealed=True)
    assert await committed(engine, session_id) == ["journal 1", "journal 2", "journal 3"]
    async with ingress(engine) as stub:
        # A sealed Session takes nothing more: the announcement is answered and the call ends.
        call = await push(stub, [announce(session_id, last_cursor=3)])
        await call.acknowledged(3, sealed=True)
        assert await call.call.read() is grpc.aio.EOF


@pytest.mark.parametrize("sealing_incarnation", [None, "test-incarnation-1"])
async def test_a_later_incarnation_continues_a_sealed_journal(
    engine: AsyncEngine, sealing_incarnation: str | None
) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(
            stub, [announce(session_id, last_cursor=2), *entries(1, 2), sealed(2, incarnation_uid=sealing_incarnation)]
        )
        await call.acknowledged(2, sealed=True)
        call = await push(stub, [announce(session_id, last_cursor=3, incarnation_uid="test-incarnation-2")])
        await call.acknowledged(2)
        await call.send(*entries(3))
        await call.acknowledged(3)
        await call.send(sealed(3, incarnation_uid="test-incarnation-2"))
        await call.acknowledged(3, sealed=True)
    assert await committed(engine, session_id) == ["journal 1", "journal 2", "journal 3"]
    page = await Store(engine).read_page(session_id)
    assert (page.feed_state.sealed.cursor, page.feed_state.sealed.incarnation_uid) == (3, "test-incarnation-2")


async def test_a_later_incarnation_announcing_at_the_seal_cursor_replaces_the_snapshot(engine: AsyncEngine) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(
            stub, [announce(session_id, last_cursor=2), *entries(1, 2), sealed(2, incarnation_uid="test-incarnation-1")]
        )
        await call.acknowledged(2, sealed=True)
        # A resumed runner has appended nothing yet, so it announces the cursor the seal ended at.
        call = await push(stub, [announce(session_id, last_cursor=2, incarnation_uid="test-incarnation-2")])
        await call.acknowledged(2)
        await call.send(*entries(3))
        await call.acknowledged(3)
    page = await Store(engine).read_page(session_id)
    assert page.feed_state.attached.incarnation_uid == "test-incarnation-2"
    assert not page.feed_state.HasField("sealed")


async def test_the_incarnation_that_sealed_a_journal_gets_the_seal_back(engine: AsyncEngine) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(
            stub, [announce(session_id, last_cursor=1), *entries(1), sealed(1, incarnation_uid="test-incarnation-1")]
        )
        await call.acknowledged(1, sealed=True)
        call = await push(stub, [announce(session_id, last_cursor=1, incarnation_uid="test-incarnation-1")])
        await call.acknowledged(1, sealed=True)
        assert await call.call.read() is grpc.aio.EOF


async def test_resume_announce_at_stopped_cursor_accepts_the_next_journal_entry(engine: AsyncEngine) -> None:
    session_id = uuid4()
    stopped = announce(session_id, last_cursor=1)
    stopped.announce.harness_state = runner_pb2.HARNESS_STATE_STOPPED
    async with ingress(engine) as stub:
        call = await push(stub, [stopped, *entries(1)])
        await call.acknowledged(1)
        call.call.cancel()
        resumed = announce(session_id, last_cursor=1)
        resumed.announce.harness_state = runner_pb2.HARNESS_STATE_RUNNING
        call = await push(stub, [resumed, *entries(2)])
        await call.acknowledged(2)
        call.call.cancel()
    page = await Store(engine).read_page(session_id, after_cursor=1)
    assert page.feed_state.attached == resumed.announce
    assert list(page.entries) == [entry(2)]


@pytest.mark.parametrize(
    ("frames", "code"),
    [
        pytest.param(entries(3), grpc.StatusCode.INVALID_ARGUMENT, id="first-entry-leaves-a-gap"),
        pytest.param(entries(1, 3), grpc.StatusCode.INVALID_ARGUMENT, id="entries-skip-a-cursor"),
        pytest.param([*entries(1), sealed(2)], grpc.StatusCode.INVALID_ARGUMENT, id="seal-past-the-last-entry"),
        pytest.param(entries(1, text="rewritten"), grpc.StatusCode.FAILED_PRECONDITION, id="replay-differs"),
    ],
)
async def test_out_of_order_and_conflicting_pushes_are_refused(
    engine: AsyncEngine, frames: list[runner_ingress_pb2.PushFrame], code: grpc.StatusCode
) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        first = await push(stub, [announce(session_id), *entries(1)])
        await first.acknowledged(1)
        first.call.cancel()
        call = await push(stub, [announce(session_id)])
        await call.acknowledged(1)
        await call.send(*frames)
        assert await call.refused() == code
    assert await committed(engine, session_id) == ["journal 1"]


@pytest.mark.parametrize("token", ["test-token-sandbox-b", "test-token-sandbox-a-recreated"])
async def test_a_session_takes_pushes_only_from_its_first_writer(engine: AsyncEngine, token: str) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        first = await push(stub, [announce(session_id), *entries(1)])
        await first.acknowledged(1)
        other = Call(stub, token)
        await other.send(announce(session_id), *entries(1, 2))
        assert await other.refused() == grpc.StatusCode.PERMISSION_DENIED
    assert await committed(engine, session_id) == ["journal 1"]


async def test_an_unknown_bearer_is_unauthenticated(engine: AsyncEngine) -> None:
    async with ingress(engine) as stub:
        call = Call(stub, "test-token-unknown")
        await call.send(announce(uuid4()))
        assert await call.refused() == grpc.StatusCode.UNAUTHENTICATED


async def test_an_idle_call_repeats_its_acknowledgement_and_a_silent_one_ends(engine: AsyncEngine) -> None:
    session_id = uuid4()
    async with ingress(engine) as stub:
        call = await push(stub, [announce(session_id), *entries(1)])
        await call.acknowledged(1)
        # Heartbeats keep the call open past the silence limit; each idle interval repeats the ack.
        for _ in range(4):
            await call.acknowledged(1)
            await call.send(runner_ingress_pb2.PushFrame(heartbeat=runner_ingress_pb2.Heartbeat()))
        assert await call.refused() == grpc.StatusCode.UNAVAILABLE


if __name__ == "__main__":
    pytest_bazel.main()
