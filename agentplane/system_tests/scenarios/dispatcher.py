"""Dispatcher queue/Ready system test, independent of the app's HTTP surface."""

import asyncio
import json
import re
import signal
import sqlite3
import sys
import tarfile
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, closing
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import grpc
import pytest
import yaml
from kubernetes_asyncio import client as k8s
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.environments.v1 import environment_service_pb2 as environments_pb, environment_service_pb2_grpc
from agentplane.grpc.calls import dial
from agentplane.proto import environment_lifecycle_pb2 as lifecycle_pb, event_log_pb2, identity_pb2, session_pb2
from agentplane.session_dispatcher.dispatcher.api.session import (
    session_dispatcher_pb2 as pb,
    session_dispatcher_pb2_grpc,
)
from agentplane.system_tests.driver.cleanup_diagnostics import capture_without_masking, cleanup_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_operations import session_until
from agentplane.system_tests.driver.dispatcher_transport import forward, service_caller
from agentplane.system_tests.driver.identity import LAUNCH_DEFAULT_POLICY_SETS, scenario_identity
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_NAME, NATIVE_CLIENT_SVID_SECRET
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.sandbox_cleanup import delete_sandbox
from agentplane.system_tests.driver.sandbox_diagnostics import (
    archive_runner_data,
    capture_cleanup_diagnostics,
    container_mount_sources,
)
from agentplane.system_tests.driver.sandbox_object import create_sandbox_object
from agentplane.system_tests.driver.tls import service_credentials, service_svid_credentials
from agentplane.system_tests.driver.tracing import tracer
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

WORKLOAD = "sessions"
BOUND_SECONDS = 300
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


class DispatchFailedError(RuntimeError):
    pass


async def _delete_pod(cluster: Cluster, name: str, uid: str) -> None:
    async with cluster.api_client() as api:
        await k8s.CoreV1Api(api).delete_namespaced_pod(
            name, cluster.namespace, body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid))
        )
    await cluster.kubectl("wait", f"pod/{name}", "--for=delete", "--timeout=120s")


async def _cleanup_sandbox(
    crictl_command: tuple[str, ...],
    cluster: Cluster,
    name: str,
    uid: str,
    *,
    session_ids: list[str],
    command_ids: list[str],
) -> None:
    try:
        await delete_sandbox(cluster, name, uid)
    except Exception as error:
        await capture_without_masking(
            error, lambda: capture_cleanup_diagnostics(crictl_command, cluster, name, session_ids, command_ids)
        )
        raise


async def _dispatch_until(stub: Dispatcher, session: str, predicate: Callable[[pb.Dispatch], bool]) -> pb.Dispatch:
    with tracer.start_as_current_span("system_test.dispatcher.admission"):
        stream = stub.WatchDispatches(
            pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=session)), timeout=BOUND_SECONDS
        )
        try:
            async for response in stream:
                if not response.HasField("dispatch"):
                    continue
                row = response.dispatch
                _normalize_delivery_state(row)
                if row.state in (pb.DELIVERY_STATE_FAILED, pb.DELIVERY_STATE_UNKNOWN):
                    raise DispatchFailedError(f"{pb.DeliveryState.Name(row.state)}: {row.reason}")
                if predicate(row):
                    return row
        finally:
            stream.cancel()
        raise AssertionError("Dispatch watch ended before the expected state")


def _normalize_delivery_state(dispatch: pb.Dispatch) -> None:
    if dispatch.state not in pb.DeliveryState.values():
        dispatch.state = pb.DELIVERY_STATE_UNKNOWN


async def _dispatcher_sql(cluster: Cluster, statement: str) -> str:
    return await cluster.exec(
        "deployment/postgres",
        "psql",
        "--host=/tmp",
        "--username=fixture_admin",
        "--dbname=session_dispatcher",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        "--set=ON_ERROR_STOP=1",
        "--command",
        statement,
    )


@asynccontextmanager
async def _held_delivery_settlement(cluster: Cluster, command: str) -> AsyncIterator[None]:
    # The test gates the real post-admission settlement; no serving-image fault switch is needed.
    command = str(UUID(command))
    process = await asyncio.create_subprocess_exec(
        *cluster.command(
            "exec",
            "--stdin",
            "deployment/postgres",
            "--",
            "psql",
            "--host=/tmp",
            "--username=fixture_admin",
            "--dbname=session_dispatcher",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    setup = f"""
CREATE FUNCTION test_hold_delivery_settlement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.command_id=TG_ARGV[0] AND NEW.state='RUNNER_ADMITTED' THEN
        PERFORM pg_advisory_xact_lock(16422, 1);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER test_hold_delivery_settlement BEFORE UPDATE ON dispatch
FOR EACH ROW EXECUTE FUNCTION test_hold_delivery_settlement('{command}');
SELECT pg_advisory_lock(16422, 1);
SELECT 'test-settlement-held';
"""
    try:
        process.stdin.write(setup.encode())
        await process.stdin.drain()
        async with asyncio.timeout(30):
            while True:
                line = await process.stdout.readline()
                assert line, "test settlement gate exited before acquiring its lock"
                if line.strip() == b"test-settlement-held":
                    break
        yield
    finally:
        if process.returncode is None:
            process.stdin.write(
                b"SELECT pg_advisory_unlock(16422, 1);\nDROP TRIGGER IF EXISTS test_hold_delivery_settlement ON dispatch;\nDROP FUNCTION IF EXISTS test_hold_delivery_settlement();\n"
            )
            await process.stdin.drain()
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, stderr.decode()


async def _wait_for_blocked_settlement(cluster: Cluster, command: str) -> dict[str, object]:
    command = str(UUID(command))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(BOUND_SECONDS),
        wait=wait_fixed(0.1),
        retry=retry_if_exception_type(AssertionError),
        reraise=True,
    ):
        with attempt:
            result = await _dispatcher_sql(
                cluster,
                f"""
SELECT json_build_object('state', d.state, 'attempted', d.delivery_attempted,
    'session_holder', l.holder,
    'settlement_blocked', EXISTS (SELECT 1 FROM pg_locks
        WHERE locktype='advisory' AND classid=16422 AND objid=1 AND NOT granted))
FROM dispatch d JOIN session_delivery_lease l USING (session_id)
WHERE d.command_id='{command}'
""",
            )
            assert result.strip(), "Session step has not acquired its lease yet"
            row: dict[str, object] = json.loads(result)
            assert row["state"] == "DELIVERING_TO_RUNNER"
            assert row["attempted"] is True
            assert row["settlement_blocked"] is True
            return row
    raise AssertionError("settlement never blocked")


@asynccontextmanager
async def _crashed_dispatcher_process(
    cluster: Cluster, pod: dict[str, Any], crictl_command: tuple[str, ...]
) -> AsyncIterator[None]:
    (container,) = [status for status in pod["status"]["containerStatuses"] if status["name"] == WORKLOAD]
    container_id = container["containerID"].removeprefix("containerd://")
    crictl_config = undeclared_outputs_dir() / "dispatcher-crash-crictl.yaml"
    crictl_config.write_text("runtime-endpoint: unix:///run/k3s/containerd/containerd.sock\ntimeout: 15\n")
    inspect = await asyncio.create_subprocess_exec(
        *crictl_command,
        "--config",
        str(crictl_config),
        "inspect",
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(inspect.communicate(), timeout=15)
    assert inspect.returncode == 0, stderr.decode()
    inspected = json.loads(stdout)
    assert inspected["status"]["id"] == container_id
    pid = inspected["info"]["pid"]
    assert isinstance(pid, int)
    assert pid > 1
    executable = await asyncio.to_thread(Path(f"/proc/{pid}/exe").readlink)
    assert executable.name == "runner-gateway", executable

    async def send_signal(name: str) -> None:
        command = await asyncio.create_subprocess_exec(
            crictl_command[0],
            "ctr",
            "--address",
            "/run/k3s/containerd/containerd.sock",
            "-n",
            "k8s.io",
            "tasks",
            "kill",
            "--signal",
            name,
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(command.communicate(), timeout=15)
        assert command.returncode == 0, stderr.decode()

    # Keep the proc file open so PID reuse cannot change which task is observed.
    with await asyncio.to_thread(Path(f"/proc/{pid}/status").open) as process_status:

        def read_status() -> str:
            process_status.seek(0)
            return process_status.read()

        try:
            await send_signal("SIGSTOP")
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(5),
                wait=wait_fixed(0.01),
                retry=retry_if_exception_type(AssertionError),
                reraise=True,
            ):
                with attempt:
                    assert re.search(r"^State:\s+T ", await asyncio.to_thread(read_status), re.MULTILINE)
            yield
            # Kubelet must observe deletion before SIGKILL, so it does not restart this container.
            async with asyncio.timeout(15):
                while True:
                    status = await asyncio.to_thread(read_status)
                    assert re.search(r"^State:\s+T ", status, re.MULTILINE), status
                    pending = re.findall(r"^(?:SigPnd|ShdPnd):\s+([0-9a-f]+)", status, re.MULTILINE)
                    if any(int(mask, 16) & (1 << (signal.SIGTERM - 1)) for mask in pending):
                        break
                    await asyncio.sleep(0.01)
            (undeclared_outputs_dir() / "dispatcher-mid-delivery-crash.json").write_text(
                json.dumps(
                    {
                        "pod_uid": pod["metadata"]["uid"],
                        "container_id": container_id,
                        "pid": pid,
                        "pending_sigterm_status": status,
                    },
                    indent=2,
                )
                + "\n"
            )
        finally:
            await send_signal("SIGKILL")


async def _wait_for_dispatcher_replicas(cluster: Cluster, count: int) -> list[dict[str, Any]]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.25), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            pods = json.loads(
                await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json")
            )
            live = [item for item in pods["items"] if not item["metadata"].get("deletionTimestamp")]
            assert len(live) == count, f"expected {count} live Sessions replicas, found {len(live)}"
            for pod in live:
                assert any(
                    condition["type"] == "Ready" and condition["status"] == "True"
                    for condition in pod["status"].get("conditions", [])
                ), f"Sessions Pod {pod['metadata']['name']} is not Ready"
            return live
    raise AssertionError("Sessions replicas never became ready")


@asynccontextmanager
async def _replace_original_with_peer(
    cluster: Cluster, deployment_uid: str, evidence: dict[str, object], *, crictl_command: tuple[str, ...] | None = None
) -> AsyncIterator[str]:
    with tracer.start_as_current_span("system_test.dispatcher.peer_lifetime"):
        (original,) = await _wait_for_dispatcher_replicas(cluster, 1)
        original_uid = original["metadata"]["uid"]
        scaled_generation: int | None = None
        try:
            patched = json.loads(
                await cluster.kubectl(
                    "patch",
                    "deployment",
                    WORKLOAD,
                    "--type=json",
                    "-p",
                    json.dumps(
                        [
                            {"op": "test", "path": "/metadata/uid", "value": deployment_uid},
                            {"op": "test", "path": "/spec/replicas", "value": 1},
                            {"op": "replace", "path": "/spec/replicas", "value": 2},
                        ]
                    ),
                    "-o",
                    "json",
                )
            )
            scaled_generation = patched["metadata"]["generation"]
            live = await _wait_for_dispatcher_replicas(cluster, 2)
            assert any(pod["metadata"]["uid"] == original_uid for pod in live), "original Sessions replica disappeared"
            (peer,) = [pod for pod in live if pod["metadata"]["uid"] != original_uid]
            if crictl_command is None:
                await _delete_pod(cluster, original["metadata"]["name"], original_uid)
            else:
                async with _crashed_dispatcher_process(cluster, original, crictl_command), cluster.api_client() as api:
                    await k8s.CoreV1Api(api).delete_namespaced_pod(
                        original["metadata"]["name"],
                        cluster.namespace,
                        body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=original_uid)),
                    )
                await cluster.kubectl("wait", f"pod/{original['metadata']['name']}", "--for=delete", "--timeout=120s")
            evidence["original_dispatcher_pod_uid"] = original_uid
            evidence["peer_dispatcher_pod_uid"] = peer["metadata"]["uid"]
            yield peer["metadata"]["name"]
        finally:
            if scaled_generation is not None:
                current = json.loads(await cluster.kubectl("get", "deployment", WORKLOAD, "-o", "json"))
                if (
                    current["metadata"]["uid"] == deployment_uid
                    and current["metadata"]["generation"] == scaled_generation
                ):
                    await cluster.kubectl(
                        "patch",
                        "deployment",
                        WORKLOAD,
                        "--type=json",
                        "-p",
                        json.dumps(
                            [
                                {"op": "test", "path": "/metadata/uid", "value": deployment_uid},
                                {"op": "test", "path": "/metadata/generation", "value": scaled_generation},
                                {"op": "test", "path": "/spec/replicas", "value": 2},
                                {"op": "replace", "path": "/spec/replicas", "value": 1},
                            ]
                        ),
                    )
                    await cluster.kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")


async def deployed_dispatcher_waits_for_ready_then_records_runner_admission(
    crictl_command: tuple[str, ...], cluster: Cluster, replica_failover: bool, *, crash_mid_delivery: bool = False
) -> None:
    deployment = json.loads(await cluster.kubectl("get", "deployment", WORKLOAD, "-o", "json"))
    assert deployment["status"].get("readyReplicas") == 1, "the Dispatcher is not Ready"
    assert deployment["spec"]["replicas"] == 1
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    policies = list(LAUNCH_DEFAULT_POLICY_SETS)
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"systest-dispatch-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": cluster.namespace,
        "image": image,
        "sandbox": sandbox,
        "session": session,
        "command": command,
        "passed": False,
        "policy_sets": policies,
    }
    path = undeclared_outputs_dir() / "dispatcher-deployed.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    async with AsyncExitStack() as stack:
        authentication, caller, _caller_uid = await service_caller(
            cluster, NATIVE_CLIENT_NAME, certificate_secret=NATIVE_CLIENT_SVID_SECRET
        )
        identities = await stack.enter_async_context(AsyncExitStack())
        identity = await identities.enter_async_context(scenario_identity(cluster, sandbox, policies))
        resource = await create_sandbox_object(
            cluster, sandbox, template=POD_TEMPLATE, identity=identity, running=False
        )
        uid = resource["metadata"]["uid"]
        evidence["sandbox_uid"] = uid
        cleaned = False

        async def cleanup(error: BaseException | None = None) -> None:
            if not cleaned:
                if isinstance(error, Exception):
                    await capture_without_masking(
                        error,
                        lambda: capture_cleanup_diagnostics(crictl_command, cluster, sandbox, [session], [command]),
                    )
                await _cleanup_sandbox(
                    crictl_command, cluster, sandbox, uid, session_ids=[session], command_ids=[command]
                )

        async def cleanup_on_exit(
            _exception_type: type[BaseException] | None, error: BaseException | None, _traceback: object
        ) -> bool:
            await cleanup_without_masking(error, lambda: cleanup(error))
            return False

        stack.push_async_exit(cleanup_on_exit)
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            "--for=jsonpath={.status.stopStage}=Stopped",
            "--timeout=300s",
        )
        server_credentials = authentication.channel_credentials
        target = await stack.enter_async_context(forward(cluster))
        channel = await stack.enter_async_context(
            dial(
                target,
                channel_credentials=server_credentials,
                server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
            )
        )
        stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as retired_environment_api:
            await environment_service_pb2_grpc.EnvironmentServiceStub(channel).ListEnvironments(
                environments_pb.ListEnvironmentsRequest(), timeout=15
            )
        assert retired_environment_api.value.code() == grpc.StatusCode.UNIMPLEMENTED
        async with dial(
            target,
            channel_credentials=await service_credentials(cluster),
            server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
        ) as anonymous_channel:
            with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
                await session_dispatcher_pb2_grpc.SessionDispatcherStub(anonymous_channel).ListDispatches(
                    pb.ListDispatchesRequest(filter=pb.DispatchFilter(sandbox=sandbox)), timeout=15
                )
            assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED

        with pytest.raises(grpc.aio.AioRpcError) as unregistered:
            await stub.ChangeReasoningEffort(
                pb.ChangeReasoningEffortRequest(
                    expected_sandbox_uid=uid, session_id=session, command_id=command, effort="low"
                ),
                timeout=15,
            )
        assert unregistered.value.code() == grpc.StatusCode.NOT_FOUND
        spec = session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                environment_hosted=session_pb2.EnvironmentHostedPlacement(
                    environment=session_pb2.ExactEnvironment(name=sandbox, uid=uid), harness=session_pb2.HARNESS_CODEX
                )
            ),
            model="chatgpt-sub/oai-responses/gpt-6-luna",
            cwd="/state/work",
            reasoning_effort="low",
        )
        created = await stub.CreateSession(pb.CreateSessionRequest(session_id=session, spec=spec), timeout=15)
        assert created.session.status.materialization == pb.SessionStatus.SESSION_MATERIALIZATION_WAITING
        queued = await stub.ChangeReasoningEffort(
            pb.ChangeReasoningEffortRequest(session_id=session, command_id=command, effort="low"), timeout=15
        )
        assert queued.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        waiting = await session_until(stub, session, lambda row: row.status.reason == "sandbox_operator_requested_stop")
        assert waiting.placement.environment_hosted.environment.uid == uid
        assert waiting.status.delivery_blocker.kind == lifecycle_pb.DELIVERY_BLOCKER_KIND_OPERATOR_REQUESTED_STOP
        assert waiting.status.wake_assessment.state == lifecycle_pb.WAKE_STATE_DENIED
        assert waiting.status.wake_assessment.reason == lifecycle_pb.WAKE_REASON_OPERATOR_REQUESTED_STOP
        waiting_sandbox = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
        assert waiting_sandbox["spec"].get("running") is False
        assert waiting_sandbox["spec"].get("wakeGeneration", 0) == 0
        evidence["waiting_reason"] = waiting.status.reason
        caller_view = json.loads(await cluster.kubectl("get", "pod", caller, "-o", "json"))
        evidence["caller_phase"] = caller_view.get("status", {}).get("phase")
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        if replica_failover:
            # The stopped Sandbox leaves this command durably queued. Replacing the initial
            # singleton then reading through its peer exercises queue recovery without an app HTTP call.
            peer = await stack.enter_async_context(
                _replace_original_with_peer(cluster, deployment["metadata"]["uid"], evidence)
            )
            peer_target = await stack.enter_async_context(forward(cluster, f"pod/{peer}"))
            peer_channel = await stack.enter_async_context(
                dial(
                    peer_target,
                    channel_credentials=server_credentials,
                    server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                )
            )
            stub = session_dispatcher_pb2_grpc.SessionDispatcherStub(peer_channel)
            retained = await stub.GetDispatch(pb.GetDispatchRequest(command_id=command), timeout=15)
            assert retained.dispatch.created_at == queued.dispatch.created_at
            assert retained.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER

        settlement_gate = AsyncExitStack()
        await stack.enter_async_context(settlement_gate)
        if crash_mid_delivery:
            await settlement_gate.enter_async_context(_held_delivery_settlement(cluster, command))
        await cluster.kubectl(
            "patch",
            "sandbox.agentplane.allegedly.works",
            sandbox,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"running": True, "wakeGeneration": 1}}),
        )
        if crash_mid_delivery:
            evidence["blocked_settlement"] = await _wait_for_blocked_settlement(cluster, command)
            peer = await stack.enter_async_context(
                _replace_original_with_peer(
                    cluster, deployment["metadata"]["uid"], evidence, crictl_command=crictl_command
                )
            )
            await settlement_gate.aclose()
            peer_target = await stack.enter_async_context(forward(cluster, f"pod/{peer}"))
            peer_channel = await stack.enter_async_context(
                dial(
                    peer_target,
                    channel_credentials=server_credentials,
                    server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                )
            )
            stub = session_dispatcher_pb2_grpc.SessionDispatcherStub(peer_channel)

        try:
            admitted = await _dispatch_until(stub, session, lambda row: row.state == pb.DELIVERY_STATE_RUNNER_ADMITTED)
        except (DispatchFailedError, grpc.aio.AioRpcError) as error:
            failed_session = await stub.GetSession(pb.GetSessionRequest(session_id=session), timeout=15)
            evidence.update(
                {
                    "failure": str(error),
                    "session_materialization": failed_session.session.status.materialization,
                    "session_reason": failed_session.session.status.reason,
                }
            )
            path.write_text(json.dumps(evidence, indent=2) + "\n")
            raise
        assert admitted.command_id == command
        assert admitted.sandbox_uid == uid
        assert admitted.admission.event.command_admitted.command.command_id == command
        assert admitted.admission.event.command_admitted.command.change_reasoning_effort.effort == "low"
        session_status = await stub.GetSession(pb.GetSessionRequest(session_id=session), timeout=15)
        assert session_status.session.status.materialization == pb.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
        observed = await session_until(stub, session, lambda row: row.status.activity.WhichOneof("kind") == "observed")
        assert observed.status.activity.observed.session_id == session
        assert observed.status.activity.observed.WhichOneof("host") == "environment_hosted"
        evidence["observed_activity"] = str(observed.status.activity)
        if crash_mid_delivery:
            # Runtime dedupe returns the original admission after the pre-crash reply was lost.
            receipt = json.loads(
                await _dispatcher_sql(
                    cluster,
                    f"""
SELECT json_build_object('attempted',delivery_attempted,'created_at',created_at,
    'reason',reason) FROM dispatch WHERE command_id='{UUID(command)!s}'
""",
                )
            )
            assert receipt["attempted"] is True
            next_command = str(uuid4())
            await stub.ChangeReasoningEffort(
                pb.ChangeReasoningEffortRequest(session_id=session, command_id=next_command, effort="low"), timeout=15
            )
            next_admitted = await _dispatch_until(
                stub,
                session,
                lambda row: row.command_id == next_command and row.state == pb.DELIVERY_STATE_RUNNER_ADMITTED,
            )
            assert next_admitted.admission.cursor > admitted.admission.cursor
            # The real Runtime journal retains one reservation and one admission for the replayed ID.
            archive_dir = undeclared_outputs_dir() / "dispatcher-crash-journal"
            archive_dir.mkdir()
            sources = await container_mount_sources(crictl_command, cluster, sandbox, archive_dir)
            archive = await archive_runner_data(
                crictl_command, cluster, sandbox, [session], archive_dir, mount_sources=sources
            )
            with closing(sqlite3.connect(f"file:{archive['backups'][session]['path']}?mode=ro", uri=True)) as journal:
                admissions = [
                    event_log_pb2.EventEntry.FromString(payload)
                    for (payload,) in journal.execute("SELECT payload FROM event_entry")
                ]
            command_admissions = [
                entry
                for entry in admissions
                if entry.event.HasField("command_admitted")
                and entry.event.command_admitted.command.command_id == command
            ]
            assert len(command_admissions) == 1
            assert command_admissions[0] == admitted.admission
            evidence["next_command"] = next_command
            evidence["replayed_admission_cursor"] = admitted.admission.cursor
        if replica_failover:
            await cluster.kubectl(
                "patch",
                "sandbox.agentplane.allegedly.works",
                sandbox,
                "--type=merge",
                "-p",
                json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
            )
            await cluster.kubectl(
                "wait",
                f"sandbox.agentplane.allegedly.works/{sandbox}",
                "--for=jsonpath={.status.stopStage}=Stopped",
                "--timeout=300s",
            )
            suspended = await session_until(
                stub,
                session,
                lambda row: (
                    row.status.activity.WhichOneof("kind") == "environment_down"
                    and row.status.activity.environment_down.cause == pb.SessionActivity.EnvironmentDown.SUSPENDED
                ),
            )
            evidence["suspended_activity"] = str(suspended.status.activity)
            deletion_session = str(uuid4())
            deletion_command = str(uuid4())
            cleanup_sessions = [session, deletion_session]
            cleanup_commands = [command, deletion_command]
            await stub.CreateSession(pb.CreateSessionRequest(session_id=deletion_session, spec=spec), timeout=15)
            await stub.ChangeReasoningEffort(
                pb.ChangeReasoningEffortRequest(
                    expected_sandbox_uid=uid, session_id=deletion_session, command_id=deletion_command, effort="low"
                ),
                timeout=15,
            )
            live = await _wait_for_dispatcher_replicas(cluster, 2)
            assert any(pod["metadata"]["uid"] == evidence["peer_dispatcher_pod_uid"] for pod in live)
            streams = []
            server_credentials = authentication.channel_credentials
            for replica in live:
                target = await stack.enter_async_context(forward(cluster, f"pod/{replica['metadata']['name']}"))
                channel = await stack.enter_async_context(
                    dial(
                        target,
                        channel_credentials=server_credentials,
                        server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                    )
                )
                reader = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
                stream = reader.WatchDispatches(
                    pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=deletion_session)),
                    timeout=BOUND_SECONDS,
                )
                stack.callback(stream.cancel)
                first = await stream.read()
                assert isinstance(first, pb.WatchDispatchesResponse), "Replica watch ended before its initial snapshot"
                assert first.dispatch.command_id == deletion_command
                assert first.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
                streams.append(stream)
            await _cleanup_sandbox(
                crictl_command, cluster, sandbox, uid, session_ids=cleanup_sessions, command_ids=cleanup_commands
            )
            cleaned = True
            deleted = await session_until(
                stub,
                session,
                lambda row: (
                    row.status.activity.WhichOneof("kind") == "environment_down"
                    and row.status.activity.environment_down.cause == pb.SessionActivity.EnvironmentDown.DELETED
                ),
            )
            evidence["deleted_activity"] = str(deleted.status.activity)
            for stream in streams:
                while True:
                    response = await stream.read()
                    if response == grpc.aio.EOF:
                        raise AssertionError("Replica watch missed the Sandbox deletion outcome")
                    assert isinstance(response, pb.WatchDispatchesResponse)
                    if response.HasField("dispatch"):
                        _normalize_delivery_state(response.dispatch)
                    if response.HasField("dispatch") and response.dispatch.state == pb.DELIVERY_STATE_FAILED:
                        assert response.dispatch.reason == "sandbox_deleted"
                        assert response.dispatch.command_id == deletion_command
                        break
            retained = await stub.GetDispatch(pb.GetDispatchRequest(command_id=deletion_command), timeout=15)
            assert retained.dispatch.state == pb.DELIVERY_STATE_FAILED
            assert retained.dispatch.sandbox_uid == uid
            listed = await stub.ListDispatches(
                pb.ListDispatchesRequest(filter=pb.DispatchFilter(session_id=deletion_session)), timeout=15
            )
            assert [row.command_id for row in listed.dispatches] == [deletion_command]
            assert listed.dispatches[0].state == retained.dispatch.state
            evidence["deletion_observed_replicas"] = [item["metadata"]["uid"] for item in live]
        else:
            environment_target = await stack.enter_async_context(
                forward(cluster, "service/environments", remote_port=8082)
            )
            environment_channel = await stack.enter_async_context(
                dial(
                    environment_target,
                    channel_credentials=await service_svid_credentials(cluster, NATIVE_CLIENT_SVID_SECRET),
                    server_name=f"environments.{cluster.namespace}.svc.cluster.local",
                )
            )
            environments = environment_service_pb2_grpc.EnvironmentServiceStub(environment_channel)
            target_handle = identity_pb2.EnvironmentTarget(namespace=cluster.namespace, name=sandbox, uid=uid)
            next_identity = await identities.enter_async_context(
                scenario_identity(cluster, f"{sandbox}-next", policies)
            )
            change = environments_pb.SetEnvironmentIdentityRequest(target=target_handle, identity_name=next_identity)
            with pytest.raises(grpc.aio.AioRpcError) as running_change:
                await environments.SetEnvironmentIdentity(change, timeout=15)
            assert running_change.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            await environments.SuspendEnvironment(
                environments_pb.EnvironmentMutationRequest(target=target_handle), timeout=15
            )
            await cluster.kubectl(
                "wait",
                f"sandbox.agentplane.allegedly.works/{sandbox}",
                "--for=jsonpath={.status.stopStage}=Stopped",
                "--timeout=300s",
            )
            await environments.SetEnvironmentIdentity(change, timeout=15)
            rebound = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
            assert rebound["spec"]["identity"]["name"] == next_identity
            retained_session = await stub.GetSession(pb.GetSessionRequest(session_id=session), timeout=15)
            assert retained_session.session.identity == session_status.session.identity
            assert retained_session.session.identity.name == identity
            next_command = str(uuid4())
            await stub.SubmitInput(
                pb.SubmitInputRequest(
                    session_id=session, command_id=next_command, text="Input to a retained Session after rebinding."
                ),
                timeout=15,
            )
            stream = stub.WatchDispatches(
                pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=session)), timeout=BOUND_SECONDS
            )
            try:
                async for response in stream:
                    if response.HasField("dispatch") and response.dispatch.command_id == next_command:
                        _normalize_delivery_state(response.dispatch)
                        if response.dispatch.state == pb.DELIVERY_STATE_FAILED:
                            assert response.dispatch.reason == "execution_identity_changed"
                            assert not response.dispatch.HasField("admission")
                            break
                        assert response.dispatch.state in (
                            pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
                            pb.DELIVERY_STATE_DELIVERING_TO_RUNNER,
                        )
                else:
                    raise AssertionError("Dispatch watch ended before the retained Session's input failed")
            finally:
                stream.cancel()
            evidence["retained_session_after_rebind"] = {
                "command": next_command,
                "reason": "execution_identity_changed",
                "old_identity": identity,
                "new_identity": next_identity,
            }
        evidence.update(
            {
                "runner_admitted": True,
                "admission_cursor": admitted.admission.cursor,
                "opening_admitted": True,
                "passed": True,
            }
        )
        path.write_text(json.dumps(evidence, indent=2) + "\n")
    evidence["cleanup_complete"] = True
    path.write_text(json.dumps(evidence, indent=2) + "\n")


async def deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends(
    crictl_command: tuple[str, ...], cluster: Cluster
) -> None:
    deployments: dict[str, object] = {}
    for name in (WORKLOAD, "environments", NATIVE_CLIENT_NAME, "notifications"):
        deployment = json.loads(await cluster.kubectl("get", "deployment", name, "-o", "json"))
        assert deployment["status"].get("readyReplicas") == deployment["spec"]["replicas"], f"{name} is not Ready"
        deployments[name] = {
            "images": {
                container["name"]: container["image"]
                for container in deployment["spec"]["template"]["spec"]["containers"]
            },
            "init_images": {
                container["name"]: container["image"]
                for container in deployment["spec"]["template"]["spec"].get("initContainers", [])
            },
        }
    gateway_config = json.loads(await cluster.kubectl("get", "configmap", f"{WORKLOAD}-settings", "-o", "json"))
    gateway_settings = yaml.safe_load(gateway_config["data"]["settings.yaml"])
    expected_dispatcher_callers = {
        "spiffe://agentplane/app",
        "spiffe://agentplane/notifications",
        "spiffe://agentplane/actions",
    }
    assert set(gateway_settings["dispatcher_service_callers"]) == expected_dispatcher_callers

    policies = list(LAUNCH_DEFAULT_POLICY_SETS)
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"systest-wake-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": cluster.namespace,
        "images": deployments,
        "sandbox": sandbox,
        "session": session,
        "command": command,
        "dispatcher_callers": sorted(expected_dispatcher_callers),
        "passed": False,
        "policy_sets": policies,
    }
    path = undeclared_outputs_dir() / "dispatcher-wake.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    # Closed in cleanup, after the Sandbox that acts as it.
    identities = AsyncExitStack()
    identity = await identities.enter_async_context(scenario_identity(cluster, sandbox, policies))
    resource = await create_sandbox_object(
        cluster, sandbox, template=POD_TEMPLATE, identity=identity, running=True, wakeOnSend=True, idleSuspendAfter="1m"
    )
    uid = resource["metadata"]["uid"]
    evidence["sandbox_uid"] = uid
    cleaned = False

    async def cleanup(error: BaseException | None = None) -> None:
        nonlocal cleaned
        if not cleaned:
            if isinstance(error, Exception):
                await capture_without_masking(
                    error, lambda: capture_cleanup_diagnostics(crictl_command, cluster, sandbox, [session], [command])
                )
            await _cleanup_sandbox(crictl_command, cluster, sandbox, uid, session_ids=[session], command_ids=[command])
            await identities.aclose()
            cleaned = True

    try:
        await cluster.kubectl(
            "wait", f"sandbox.agentplane.allegedly.works/{sandbox}", "--for=condition=Ready=True", "--timeout=300s"
        )
        initial = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
        initial_incarnation = initial.get("status", {}).get("incarnation", {}).get("uid")
        assert initial_incarnation
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            "--for=jsonpath={.status.suspendedBy}=Idle",
            "--timeout=300s",
        )
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            "--for=jsonpath={.status.stopStage}=Stopped",
            "--timeout=300s",
        )
        stopped = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
        assert stopped["spec"]["running"] is True
        assert stopped["status"]["suspendedBy"] == "Idle"
        assert stopped["status"]["suspendedAtWakeGeneration"] == stopped["spec"]["wakeGeneration"] == 0
        evidence["initial_incarnation"] = initial_incarnation
        app_authentication, app_pod, app_pod_uid = await service_caller(
            cluster, NATIVE_CLIENT_NAME, certificate_secret=NATIVE_CLIENT_SVID_SECRET
        )
        # This is the Dispatcher submission-authorization case, not a Runtime-proxy reader.
        notification_authentication, notification_pod, notification_pod_uid = await service_caller(
            cluster, "notifications", certificate_secret="notifications-workload-tls"
        )
        evidence["caller_pods"] = {
            "app": {"name": app_pod, "uid": app_pod_uid},
            "notifications": {"name": notification_pod, "uid": notification_pod_uid},
        }
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        server_credentials = app_authentication.channel_credentials
        async with forward(cluster) as target:
            channel = dial(
                target,
                channel_credentials=server_credentials,
                server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
            )
            try:
                stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
                notification_channel = dial(
                    target,
                    channel_credentials=notification_authentication.channel_credentials,
                    server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                )
                notification_stub = session_dispatcher_pb2_grpc.SessionDispatcherStub(notification_channel)
                created = await stub.CreateSession(
                    pb.CreateSessionRequest(
                        session_id=session,
                        spec=session_pb2.SessionSpec(
                            placement=session_pb2.SessionPlacement(
                                environment_hosted=session_pb2.EnvironmentHostedPlacement(
                                    environment=session_pb2.ExactEnvironment(name=sandbox, uid=uid),
                                    harness=session_pb2.HARNESS_CODEX,
                                )
                            ),
                            model="chatgpt-sub/oai-responses/gpt-6-luna",
                            cwd="/state/work",
                            reasoning_effort="low",
                            instructions=f"EARLY_DIAGNOSTIC_PAYLOAD_{sandbox}",
                        ),
                    ),
                    timeout=15,
                )
                queued = await notification_stub.SubmitInput(
                    pb.SubmitInputRequest(
                        expected_sandbox_uid=uid,
                        session_id=session,
                        command_id=command,
                        text=f"LATE_DIAGNOSTIC_PAYLOAD_{sandbox} Reply with exactly OK and do not use tools.",
                    ),
                    timeout=15,
                )
                assert created.session.status.materialization == pb.SessionStatus.SESSION_MATERIALIZATION_WAITING
                assert queued.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
                await cluster.kubectl(
                    "wait",
                    f"sandbox.agentplane.allegedly.works/{sandbox}",
                    "--for=jsonpath={.spec.wakeGeneration}=1",
                    "--timeout=120s",
                )
                waking = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
                assert waking["spec"]["running"] is True
                assert waking["spec"]["wakeGeneration"] == 1
                evidence["wake_generation"] = waking["spec"]["wakeGeneration"]
                path.write_text(json.dumps(evidence, indent=2) + "\n")

                await cluster.kubectl(
                    "wait",
                    f"sandbox.agentplane.allegedly.works/{sandbox}",
                    "--for=condition=Ready=True",
                    "--timeout=300s",
                )
                ready = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
                ready_incarnation = ready.get("status", {}).get("incarnation", {}).get("uid")
                assert ready_incarnation
                assert ready_incarnation != initial_incarnation
                admitted = await _dispatch_until(
                    notification_stub, session, lambda row: row.state == pb.DELIVERY_STATE_RUNNER_ADMITTED
                )
                assert admitted.command_id == command
                assert admitted.admission.event.command_admitted.command.submit_input.text == (
                    f"LATE_DIAGNOSTIC_PAYLOAD_{sandbox} Reply with exactly OK and do not use tools."
                )
                async for attempt in AsyncRetrying(
                    stop=stop_after_delay(BOUND_SECONDS),
                    wait=wait_fixed(1),
                    retry=retry_if_exception_type(AssertionError),
                    reraise=True,
                ):
                    with attempt:
                        session_status = await stub.GetSession(pb.GetSessionRequest(session_id=session), timeout=15)
                        materialization = session_status.session.status.materialization
                        if materialization in (
                            pb.SessionStatus.SESSION_MATERIALIZATION_FAILED,
                            pb.SessionStatus.SESSION_MATERIALIZATION_UNKNOWN,
                        ):
                            raise DispatchFailedError(
                                f"Session materialization {materialization}: {session_status.session.status.reason}"
                            )
                        assert materialization == pb.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED, (
                            f"Session did not reach owner acceptance: {materialization}"
                        )

                capture_id = uuid4().hex
                archive_dir = undeclared_outputs_dir() / f"dispatcher-data-proof-{sandbox}-{capture_id}"
                archive_dir.mkdir(parents=True)
                mount_sources = await container_mount_sources(crictl_command, cluster, sandbox, archive_dir)
                state_session = mount_sources["/state"] / "sessions" / session
                early_payload = f"EARLY_ARCHIVE_PAYLOAD_{sandbox}".encode()
                late_payload = f"LATE_ARCHIVE_PAYLOAD_{sandbox}".encode()
                late_session_payload = f"LATE_DIAGNOSTIC_PAYLOAD_{sandbox}".encode()
                (state_session / "diagnostic-early.bin").write_bytes(early_payload)
                (mount_sources["/home/runner"] / f"diagnostic-late-{sandbox}.bin").write_bytes(late_payload)

                wal_path = mount_sources["/tmp"] / f"dispatcher-wal-{capture_id}.sqlite"
                wal_db = sqlite3.connect(wal_path)
                try:
                    assert wal_db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
                    wal_db.execute("PRAGMA wal_autocheckpoint=0")
                    wal_db.execute("CREATE TABLE event_entry (cursor INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
                    wal_db.executemany(
                        "INSERT INTO event_entry(cursor, payload) VALUES (?, ?)",
                        [(1, early_payload), (2, late_payload)],
                    )
                    wal_db.commit()
                    wal_file = Path(f"{wal_path}-wal")
                    assert await asyncio.to_thread(wal_file.is_file)
                    wal_before = await asyncio.to_thread(wal_file.stat)
                    assert wal_before.st_size > 32
                    archive_manifest = await archive_runner_data(
                        crictl_command,
                        cluster,
                        sandbox,
                        [session],
                        archive_dir,
                        mount_sources=mount_sources,
                        extra_backups=(("wal_probe", wal_path),),
                    )
                    assert archive_manifest["archive"] == "complete", archive_manifest
                    session_backup = Path(archive_manifest["backups"][session]["path"])
                    probe_backup = Path(archive_manifest["backups"]["wal_probe"]["path"])
                    probe = sqlite3.connect(f"file:{probe_backup}?mode=ro", uri=True)
                    try:
                        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                        assert probe.execute("SELECT cursor, payload FROM event_entry ORDER BY cursor").fetchall() == [
                            (1, early_payload),
                            (2, late_payload),
                        ]
                    finally:
                        probe.close()
                    # Online backup must leave the WAL untouched and the live writer usable.
                    wal_after_backup = await asyncio.to_thread(wal_file.stat)
                    assert wal_after_backup.st_size == wal_before.st_size
                    assert wal_after_backup.st_mtime_ns == wal_before.st_mtime_ns
                    wal_db.execute("INSERT INTO event_entry(cursor, payload) VALUES (3, ?)", (b"after-backup",))
                    wal_db.commit()
                    session_db = sqlite3.connect(f"file:{session_backup}?mode=ro", uri=True)
                    try:
                        journal_payloads = [
                            value
                            for (value,) in session_db.execute("SELECT payload FROM event_entry")
                            if isinstance(value, bytes)
                        ]
                        assert any(late_session_payload in value for value in journal_payloads)
                    finally:
                        session_db.close()
                    found_markers: set[str] = set()
                    expected = {
                        f"/sessions/{session}/diagnostic-early.bin": early_payload,
                        f"/diagnostic-late-{sandbox}.bin": late_payload,
                    }
                    with tarfile.open(archive_manifest["archive_path"], "r|") as runner_archive:
                        for member in runner_archive:
                            if not member.isfile():
                                continue
                            for suffix, expected_payload in expected.items():
                                if member.name.endswith(suffix):
                                    archived_file = runner_archive.extractfile(member)
                                    assert archived_file is not None
                                    archived_payload = archived_file.read()
                                    assert archived_payload == expected_payload
                                    found_markers.add(suffix)
                    assert found_markers == set(expected)
                finally:
                    wal_db.close()
                evidence.update(
                    {
                        "ready_incarnation": ready_incarnation,
                        "session_materialization": session_status.session.status.materialization,
                        "dispatch_state": pb.DeliveryState.Name(admitted.state),
                        "dispatch_admission_cursor": admitted.admission.cursor,
                        "runner_data_archive": archive_manifest["archive_path"],
                        "runner_data_manifest": str(archive_dir / "archive-manifest.json"),
                        "passed": True,
                    }
                )
                path.write_text(json.dumps(evidence, indent=2) + "\n")
            finally:
                await channel.close()
                await notification_channel.close()
    finally:
        failure = sys.exception()
        await cleanup_without_masking(failure, lambda: cleanup(failure))
    evidence["cleanup_complete"] = True
    path.write_text(json.dumps(evidence, indent=2) + "\n")


# gazelle:include_dep @pypi//protobuf
