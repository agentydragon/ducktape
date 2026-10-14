"""Fresh Dispatcher service lifecycles retain work in their own PostgreSQL with no app process."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s

from agentplane.grpc_calls import dial
from agentplane.proto import runner_pb2, session_dispatcher_pb2 as pb, session_dispatcher_pb2_grpc
from agentplane.sandboxes.testing.kubernetes import OWNER, SANDBOX, SANDBOX_UID, SandboxCluster
from agentplane.session_dispatcher import main as dispatcher_main
from agentplane.session_dispatcher.settings import Settings
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.session_dispatcher.testing.scripted_runner import ScriptedRunner
from agentplane.testing.fake_apiserver import TokenVerdict
from util.net import pick_free_port

# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

AUDIENCE = "test-dispatcher-startup"
TOKEN = "test-only-caller-token"
SESSION = "test-session"
COMMAND = "retained-command"
METADATA = (("authorization", f"Bearer {TOKEN}"),)
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


async def test_service_restart_after_lease_expiry_retains_queued_work_without_an_app(
    cluster: SandboxCluster,
    db_url: str,
    runner: tuple[ScriptedRunner, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_sandbox(cluster, ready=False, running=False)
    cluster.fake.tokens[TOKEN] = TokenVerdict(
        username=f"system:serviceaccount:{OWNER.namespace}:{OWNER.name}",
        pod_name="test-caller",
        pod_uid="test-caller-uid",
        audiences=(AUDIENCE,),
    )
    (runner_token := tmp_path / "runner-token").write_text("test-only-runner-token")
    settings = Settings(
        database_url=db_url,
        namespace=OWNER.namespace,
        caller_accounts=frozenset({OWNER}),
        token_audience=AUDIENCE,
        runner_control_target=runner[1],
        runner_token_file=runner_token,
        runner_request_timeout_s=5,
        request_timeout_s=5,
        host="127.0.0.1",
        port=pick_free_port(),
        _cli_parse_args=False,
    )

    async def configuration(path: Path | None) -> k8s.Configuration:
        del path
        return cluster.api.configuration

    monkeypatch.setattr(dispatcher_main, "kubernetes_configuration", configuration)

    @asynccontextmanager
    async def running() -> AsyncIterator[Dispatcher]:
        stopping = asyncio.Event()

        async def until_stopped() -> None:
            await stopping.wait()

        monkeypatch.setattr(dispatcher_main, "until_terminated", until_stopped)
        task = asyncio.create_task(dispatcher_main.serve(settings), name="dispatcher-service")
        try:
            async with dial(f"127.0.0.1:{settings.port}") as channel:
                async with asyncio.timeout(5):
                    await channel.channel_ready()
                stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
                yield stub
        finally:
            stopping.set()
            await task

    # Each lifecycle constructs and disposes its own engine, store, Sandbox watch, auth resolver,
    # gRPC server and delivery worker through the production entry point. The app is never started.
    async with running() as first:
        opened = await first.OpenSession(
            pb.OpenSessionRequest(sandbox=SANDBOX, session_id=SESSION, spec=runner_pb2.SessionSpec()),
            metadata=METADATA,
            timeout=5,
        )
        assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        queued = (
            await first.SubmitInput(
                pb.SubmitInputRequest(command_id=COMMAND, session_id=SESSION, sandbox=SANDBOX, text="fixture input"),
                metadata=METADATA,
                timeout=5,
            )
        ).dispatch
        assert queued.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        assert runner[0].calls == []

    # A stopped process no longer renews its scheduling Lease. Model the expiry instead of waiting
    # out the production lease duration before allowing the replacement to take ownership.
    coordination = k8s.CoordinationV1Api(cluster.api)
    lease_name = f"dispatcher-{UUID(SANDBOX_UID).hex}"
    lease = await coordination.read_namespaced_lease(lease_name, OWNER.namespace)
    lease.spec.renew_time = datetime.now(UTC) - timedelta(seconds=60)
    duration = lease.spec.lease_duration_seconds or 30
    assert lease.spec.renew_time + timedelta(seconds=duration) <= datetime.now(UTC)
    await coordination.replace_namespaced_lease(lease_name, OWNER.namespace, lease)

    async with running() as restarted:
        retained = (
            await restarted.GetDispatch(pb.GetDispatchRequest(command_id=COMMAND), metadata=METADATA, timeout=5)
        ).dispatch
        assert retained.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        assert retained.created_at == queued.created_at
        assert retained.sandbox_uid == queued.sandbox_uid
        assert runner[0].calls == []
        set_sandbox(cluster, ready=True)
        stream = restarted.WatchDispatches(
            pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=SESSION)), metadata=METADATA, timeout=5
        )
        try:
            async for response in stream:
                assert response.dispatch.state != pb.DELIVERY_STATE_FAILED, response.dispatch.reason
                if response.dispatch.state == pb.DELIVERY_STATE_RUNNER_ADMITTED:
                    break
            else:
                raise AssertionError("retained command did not reach the runner")
        finally:
            stream.cancel()
        assert runner[0].calls == [("OpenSession", SESSION), ("SubmitInput", COMMAND)]
        assert cluster.fake.token_reviews >= 4


if __name__ == "__main__":
    pytest_bazel.main()
