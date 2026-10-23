"""Dispatcher queue/Ready system test, independent of the app's HTTP surface."""

import asyncio
import json
import re
import sqlite3
import sys
import tarfile
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

import grpc
import pytest
import yaml
from kubernetes_asyncio import client as k8s, watch as k8s_watch

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import dial
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2 as pb, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.cleanup_diagnostics import capture_without_masking, cleanup_without_masking
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import POD_TEMPLATE
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

SERVICE = "session-dispatcher"
WORKLOAD = "runner-gateway"
BOUND_SECONDS = 300
CLEANUP_DIAGNOSTICS_SECONDS = 30
RUNNER_DATA_CAPTURE_SECONDS = 120
RUNNER_DATA_MOUNTS = ("/state", "/state/workspaces", "/home/runner", "/tmp")
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


@dataclass(frozen=True)
class Caller:
    token: str = field(repr=False)

    @property
    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),)


class DispatchFailedError(RuntimeError):
    pass


async def _delete_pod(cluster: Cluster, name: str, uid: str) -> None:
    await _delete(cluster, "pod", name, uid)


async def _bound_caller(cluster: Cluster, service_account: str) -> tuple[Caller, str, str]:
    pods = json.loads(
        await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={service_account}", "-o", "json")
    )
    pod = next(item for item in pods["items"] if not item["metadata"].get("deletionTimestamp"))
    name = pod["metadata"]["name"]
    uid = pod["metadata"]["uid"]
    token = await cluster.kubectl(
        "create",
        "token",
        service_account,
        f"--audience={SERVICE}",
        "--duration=600s",
        "--bound-object-kind=Pod",
        f"--bound-object-name={name}",
        f"--bound-object-uid={uid}",
    )
    return Caller(token), name, uid


async def _delete(cluster: Cluster, kind: str, name: str, uid: str) -> None:
    async with cluster.api_client() as api:
        options = k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid))
        if kind == "pod":
            await k8s.CoreV1Api(api).delete_namespaced_pod(name, cluster.namespace, body=options)
        else:
            await k8s.CustomObjectsApi(api).delete_namespaced_custom_object(
                GROUP, VERSION, cluster.namespace, "sandboxes", name, body=options
            )
    await cluster.kubectl("wait", f"{kind}/{name}", "--for=delete", "--timeout=120s")


async def _container_mount_sources(
    crictl_command: tuple[str, ...], cluster: Cluster, name: str, artifact_dir: Path
) -> dict[str, Path]:
    pod = json.loads(await cluster.kubectl("get", "pod", name, "-o", "json"))
    statuses = pod.get("status", {}).get("containerStatuses", [])
    runner = next((status for status in statuses if status.get("name") == "runner"), None)
    if runner is None or not runner.get("containerID"):
        raise RuntimeError("runner container id is unavailable")
    container_id = runner["containerID"].removeprefix("containerd://")
    crictl_config = artifact_dir / "crictl.yaml"
    crictl_config.write_text("runtime-endpoint: unix:///run/k3s/containerd/containerd.sock\ntimeout: 30\n")
    process = await asyncio.create_subprocess_exec(
        *crictl_command,
        "--config",
        str(crictl_config),
        "inspect",
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(CLEANUP_DIAGNOSTICS_SECONDS):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"crictl inspect exited {process.returncode}: {stderr.decode(errors='replace')[-2000:]}")
    inspected = json.loads(stdout)
    runtime_spec = inspected.get("info", {}).get("runtimeSpec", {})
    if isinstance(runtime_spec, str):
        runtime_spec = json.loads(runtime_spec)
    mounts = runtime_spec.get("mounts", [])
    sources: dict[str, Path] = {}
    for destination in RUNNER_DATA_MOUNTS:
        mount = next((item for item in mounts if item.get("destination") == destination), None)
        if mount is None or not mount.get("source"):
            raise RuntimeError(f"container runtime did not expose source for {destination}")
        source = Path(mount["source"])
        if not await asyncio.to_thread(source.exists):
            raise FileNotFoundError(f"container mount source for {destination} is not visible: {source}")
        sources[destination] = source
    return sources


async def _archive_runner_data(
    crictl_command: tuple[str, ...],
    cluster: Cluster,
    name: str,
    session_ids: list[str],
    artifact_dir: Path,
    *,
    mount_sources: dict[str, Path] | None = None,
    extra_backups: tuple[tuple[str, Path], ...] = (),
) -> dict[str, Any]:
    sources = mount_sources or await _container_mount_sources(crictl_command, cluster, name, artifact_dir)
    archive = artifact_dir / "runner-data.tar"
    partial_archive = artifact_dir / "runner-data.tar.partial"
    journal_dir = artifact_dir / "consistent-journals"
    state_source = sources["/state"]
    backups = [
        {
            "name": session_id,
            "source": str(state_source / "sessions" / session_id / "journal.sqlite"),
            "destination": str(journal_dir / f"session-{session_id}.sqlite"),
        }
        for session_id in session_ids
    ]
    backups.extend(
        {"name": name, "source": str(source), "destination": str(journal_dir / f"{name}.sqlite")}
        for name, source in extra_backups
    )
    plan_path = artifact_dir / "archive-plan.json"
    manifest_path = artifact_dir / "archive-manifest.json"
    plan_path.write_text(
        json.dumps(
            {
                "archive": str(archive),
                "partial_archive": str(partial_archive),
                "manifest": str(manifest_path),
                "mounts": [
                    {"destination": destination, "source": str(sources[destination])}
                    for destination in RUNNER_DATA_MOUNTS
                ],
                "backups": backups,
            },
            indent=2,
        )
        + "\n"
    )
    # A separate process lets timeout/cancellation stop SQLite backup and filesystem traversal
    # before cleanup removes their sources; cancelling a worker thread would leave them running.
    process = await asyncio.create_subprocess_exec(
        str(get_required_path("_main/agentplane/system_tests/runner_data_archive_bin")),
        str(plan_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(RUNNER_DATA_CAPTURE_SECONDS):
            stdout, stderr = await process.communicate()
    except BaseException as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, TimeoutError):
            (artifact_dir / "archive-timeout.txt").write_text(
                f"Runner data capture exceeded {RUNNER_DATA_CAPTURE_SECONDS}s; partial files were retained.\n"
            )
        raise
    if not manifest_path.exists():
        raise RuntimeError(
            f"runner data archiver exited {process.returncode} without a manifest: "
            f"{stderr.decode(errors='replace')[-2000:]} {stdout.decode(errors='replace')[-2000:]}"
        )
    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    manifest["mount_sources"] = {destination: str(source) for destination, source in sources.items()}
    if process.returncode:
        manifest["archiver_exit_code"] = process.returncode
    return manifest


async def _capture_cleanup_diagnostics(
    crictl_command: tuple[str, ...], cluster: Cluster, name: str, session_ids: list[str], command_ids: list[str]
) -> None:
    capture_id = uuid4().hex
    artifact_dir = undeclared_outputs_dir() / f"dispatcher-cleanup-{name}-{capture_id}"
    artifact_dir.mkdir(parents=True)
    diagnostics: dict[str, object] = {
        "sandbox": name,
        "capture_id": capture_id,
        "session_ids": session_ids,
        "command_ids": command_ids,
    }
    diagnostic_path = artifact_dir / "diagnostics.json"

    def persist() -> None:
        diagnostic_path.write_text(json.dumps(diagnostics, indent=2) + "\n")

    persist()
    commands = {
        "sandbox": ("get", "sandbox", name, "-o", "json"),
        "runner_lease": ("get", "lease", f"{name}-runner", "-o", "json"),
        "pod": ("get", "pod", name, "-o", "json"),
        "runner_log": ("logs", f"pod/{name}", "-c", "runner", "--since=10m", "--timestamps"),
        "previous_runner_log": ("logs", f"pod/{name}", "-c", "runner", "--previous", "--since=10m", "--timestamps"),
        "sandbox_controller_log": (
            "logs",
            "deployment/sandbox-controller",
            "--all-containers",
            "--since=10m",
            "--timestamps",
        ),
    }

    async def capture(key: str, args: tuple[str, ...]) -> None:
        try:
            async with asyncio.timeout(CLEANUP_DIAGNOSTICS_SECONDS):
                value = await cluster.kubectl(*args)
            try:
                diagnostics[key] = json.loads(value)
            except json.JSONDecodeError:
                diagnostics[key] = value
        except TimeoutError:
            diagnostics[key] = f"TimeoutError: exceeded {CLEANUP_DIAGNOSTICS_SECONDS}s diagnostic deadline"
        except Exception as error:
            diagnostics[key] = {"capture_error": type(error).__name__}

    await asyncio.gather(*(capture(key, args) for key, args in commands.items()))
    persist()
    try:
        diagnostics["runner_data"] = await _archive_runner_data(
            crictl_command, cluster, name, session_ids, artifact_dir
        )
    except Exception as error:
        diagnostics["runner_data_error"] = f"{type(error).__name__}: {error}"
    persist()


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
        await cluster.kubectl(
            "patch",
            "sandbox.agentplane.allegedly.works",
            name,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
        )
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{name}",
            "--for=jsonpath={.status.operatingMode}=Suspended",
            "--timeout=300s",
        )
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{name}",
            "--for=jsonpath={.status.stopStage}=Stopped",
            "--timeout=300s",
        )
        await _delete(cluster, "sandbox.agentplane.allegedly.works", name, uid)
    except Exception as error:
        await capture_without_masking(
            error, lambda: _capture_cleanup_diagnostics(crictl_command, cluster, name, session_ids, command_ids)
        )
        raise


@asynccontextmanager
async def _forward(cluster: Cluster, resource: str = f"service/{SERVICE}") -> AsyncIterator[str]:
    async def stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            async with asyncio.timeout(10):
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def drain_output(
        process: asyncio.subprocess.Process, log_file: BinaryIO, ready: asyncio.Future[int | None]
    ) -> None:
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                log_file.write(line)
                if not ready.done() and (
                    match := re.match(rb"Forwarding from 127\.0\.0\.1:(\d+) -> 8080(?:\s|$)", line)
                ):
                    ready.set_result(int(match[1]))
        finally:
            if not ready.done():
                ready.set_result(None)

    async def stop_and_drain(process: asyncio.subprocess.Process, drain_task: asyncio.Task[None]) -> None:
        try:
            await stop(process)
        finally:
            try:
                async with asyncio.timeout(5):
                    await drain_task
            except TimeoutError:
                drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)

    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *cluster.command(),
            "port-forward",
            resource,
            ":8080",
            "--address=127.0.0.1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    )
    cancelled_during_spawn = False
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await asyncio.shield(spawn)
        cancelled_during_spawn = True
    assert process.stdout is not None
    log_path = undeclared_outputs_dir() / f"dispatcher-port-forward-{uuid4().hex}.log"
    with log_path.open("wb") as log_file:
        ready: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
        drain_task = asyncio.create_task(drain_output(process, log_file, ready))
        try:
            if cancelled_during_spawn:
                raise asyncio.CancelledError
            try:
                async with asyncio.timeout(30):
                    port = await ready
            except TimeoutError as exc:
                raise RuntimeError(f"Dispatcher port-forward did not report a listener; see {log_path}") from exc
            if port is None:
                raise RuntimeError(f"Dispatcher port-forward ended before listening; see {log_path}")
            yield f"127.0.0.1:{port}"
        finally:
            cleanup = asyncio.create_task(stop_and_drain(process, drain_task))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.shield(cleanup)
                raise


async def _dispatch_until(
    stub: Dispatcher, caller: Caller, session: str, predicate: Callable[[pb.Dispatch], bool]
) -> pb.Dispatch:
    stream = stub.WatchDispatches(
        pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=session)),
        metadata=caller.metadata,
        timeout=BOUND_SECONDS,
    )
    try:
        async for response in stream:
            if not response.HasField("dispatch"):
                continue
            row = response.dispatch
            if row.state in (pb.DELIVERY_STATE_FAILED, pb.DELIVERY_STATE_UNKNOWN):
                raise DispatchFailedError(f"{pb.DeliveryState.Name(row.state)}: {row.reason}")
            if predicate(row):
                return row
    finally:
        stream.cancel()
    raise AssertionError("Dispatch watch ended before the expected state")


@asynccontextmanager
async def _replace_owner_with_peer(
    cluster: Cluster, sandbox_uid: str, deployment_uid: str, evidence: dict[str, object]
) -> AsyncIterator[str]:
    pods = json.loads(await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json"))
    (original,) = pods["items"]
    lease_name = f"dispatcher-{UUID(sandbox_uid).hex}"
    lease = json.loads(await cluster.kubectl("get", "lease", lease_name, "-o", "json"))
    holder = lease["spec"]["holderIdentity"]
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
        await cluster.kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")
        pods = json.loads(
            await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json")
        )
        (peer,) = [item for item in pods["items"] if item["metadata"]["uid"] != original["metadata"]["uid"]]
        await _delete_pod(cluster, original["metadata"]["name"], original["metadata"]["uid"])
        async with cluster.api_client() as api, k8s_watch.Watch() as watch:
            coordination = k8s.CoordinationV1Api(api)
            current = await coordination.read_namespaced_lease(lease_name, cluster.namespace)
            if current.spec.holder_identity == holder:
                async with asyncio.timeout(BOUND_SECONDS):
                    async for event in watch.stream(
                        coordination.list_namespaced_lease,
                        cluster.namespace,
                        field_selector=f"metadata.name={lease_name}",
                        resource_version=current.metadata.resource_version,
                    ):
                        current = event["object"]
                        if current.spec.holder_identity != holder:
                            break
            assert current.spec.holder_identity != holder
            assert current.spec.lease_transitions > lease["spec"]["leaseTransitions"]
            evidence["owner_before"] = holder
            evidence["owner_after"] = current.spec.holder_identity
            evidence["replaced_dispatcher_pod_uid"] = original["metadata"]["uid"]
        yield peer["metadata"]["name"]
    finally:
        if scaled_generation is not None:
            current = json.loads(await cluster.kubectl("get", "deployment", WORKLOAD, "-o", "json"))
            # Do not overwrite a concurrent operator or GitOps rollout.
            if current["metadata"]["uid"] == deployment_uid and current["metadata"]["generation"] == scaled_generation:
                await cluster.kubectl(
                    "scale",
                    f"deployment/{WORKLOAD}",
                    "--current-replicas=2",
                    "--replicas=1",
                    f"--resource-version={current['metadata']['resourceVersion']}",
                )
                await cluster.kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")


async def deployed_dispatcher_waits_for_ready_then_records_runner_admission(
    crictl_command: tuple[str, ...], cluster: Cluster, replica_failover: bool
) -> None:
    deployment = json.loads(await cluster.kubectl("get", "deployment", WORKLOAD, "-o", "json"))
    assert deployment["status"].get("readyReplicas") == 1, "the Dispatcher is not Ready"
    assert deployment["spec"]["replicas"] == 1
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    app_config = json.loads(await cluster.kubectl("get", "configmap", "app-config", "-o", "json"))
    policies = yaml.safe_load(app_config["data"]["config.yaml"])["default_policy_sets"]
    assert isinstance(policies, list)
    assert policies
    assert all(isinstance(name, str) and name for name in policies)
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
        # TokenReview needs a bound workload identity, not an app HTTP session. Reuse an
        # existing app Pod as the token's binding object; this probe creates no caller Pods.
        authentication, caller, _caller_uid = await _bound_caller(cluster, "app")
        created = json.loads(
            await cluster.kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": sandbox, "namespace": cluster.namespace},
                    "spec": {"template": POD_TEMPLATE, "running": False, "policySets": policies},
                },
            )
        )
        uid = created["metadata"]["uid"]
        evidence["sandbox_uid"] = uid
        cleaned = False

        async def cleanup(error: BaseException | None = None) -> None:
            if not cleaned:
                if isinstance(error, Exception):
                    await capture_without_masking(
                        error,
                        lambda: _capture_cleanup_diagnostics(crictl_command, cluster, sandbox, [session], [command]),
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
        target = await stack.enter_async_context(_forward(cluster))
        channel = await stack.enter_async_context(dial(target))
        stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
            await stub.ListDispatches(pb.ListDispatchesRequest(filter=pb.DispatchFilter(sandbox=sandbox)), timeout=15)
        assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED
        queued = await stub.ChangeReasoningEffort(
            pb.ChangeReasoningEffortRequest(
                sandbox=sandbox, sandbox_uid=uid, session_id=session, command_id=command, effort="low"
            ),
            metadata=authentication.metadata,
            timeout=15,
        )
        assert queued.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        waiting = await _dispatch_until(
            stub, authentication, session, lambda row: row.reason == "wake_on_send_disabled"
        )
        assert waiting.sandbox_uid == uid
        waiting_sandbox = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
        assert waiting_sandbox["spec"].get("running") is False
        assert waiting_sandbox["spec"].get("wakeGeneration", 0) == 0
        evidence["waiting_reason"] = waiting.reason
        caller_view = json.loads(await cluster.kubectl("get", "pod", caller, "-o", "json"))
        evidence["caller_phase"] = caller_view.get("status", {}).get("phase")
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        if replica_failover:
            # Only one replica can have acquired this Sandbox's Lease before scaling.
            # Killing that known owner, then watching through the surviving peer, exercises
            # lease expiry and durable queue recovery without the app HTTP service.
            peer = await stack.enter_async_context(
                _replace_owner_with_peer(cluster, uid, deployment["metadata"]["uid"], evidence)
            )
            peer_target = await stack.enter_async_context(_forward(cluster, f"pod/{peer}"))
            peer_channel = await stack.enter_async_context(dial(peer_target))
            stub = session_dispatcher_pb2_grpc.SessionDispatcherStub(peer_channel)
            retained = await stub.GetDispatch(
                pb.GetDispatchRequest(command_id=command), metadata=authentication.metadata, timeout=15
            )
            assert retained.dispatch.created_at == queued.dispatch.created_at
            assert retained.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER

        # Opening blocks the Session's commands. Queue it after observing the command's own
        # suspended reason; it must still reach the runner before the setting change.
        opened = await stub.OpenSession(
            pb.OpenSessionRequest(
                sandbox=sandbox,
                session_id=session,
                spec=runner_pb2.SessionSpec(
                    harness=runner_pb2.HARNESS_CODEX,
                    model="chatgpt-sub/oai-responses/gpt-6-luna",
                    cwd="/state/work",
                    reasoning_effort="low",
                ),
            ),
            metadata=authentication.metadata,
            timeout=15,
        )
        assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        await cluster.kubectl(
            "patch",
            "sandbox.agentplane.allegedly.works",
            sandbox,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"running": True, "wakeGeneration": 1}}),
        )
        try:
            admitted = await _dispatch_until(
                stub, authentication, session, lambda row: row.state == pb.DELIVERY_STATE_RUNNER_ADMITTED
            )
        except (DispatchFailedError, grpc.aio.AioRpcError) as error:
            failed_opening = await stub.GetSessionOpening(
                pb.GetSessionOpeningRequest(session_id=session), metadata=authentication.metadata, timeout=15
            )
            evidence.update(
                {
                    "failure": str(error),
                    "opening_state": pb.DeliveryState.Name(failed_opening.opening.state),
                    "opening_reason": failed_opening.opening.reason,
                }
            )
            path.write_text(json.dumps(evidence, indent=2) + "\n")
            raise
        assert admitted.command_id == command
        assert admitted.sandbox_uid == uid
        assert admitted.admission.event.command_admitted.command.command_id == command
        assert admitted.admission.event.command_admitted.command.change_reasoning_effort.effort == "low"
        opening = await stub.GetSessionOpening(
            pb.GetSessionOpeningRequest(session_id=session), metadata=authentication.metadata, timeout=15
        )
        assert opening.opening.state == pb.DELIVERY_STATE_RUNNER_ADMITTED
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
            deletion_session = str(uuid4())
            deletion_command = str(uuid4())
            cleanup_sessions = [session, deletion_session]
            cleanup_commands = [command, deletion_command]
            await stub.ChangeReasoningEffort(
                pb.ChangeReasoningEffortRequest(
                    sandbox=sandbox,
                    sandbox_uid=uid,
                    session_id=deletion_session,
                    command_id=deletion_command,
                    effort="low",
                ),
                metadata=authentication.metadata,
                timeout=15,
            )
            await cluster.kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")
            pods = json.loads(
                await cluster.kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json")
            )
            live = [item for item in pods["items"] if not item["metadata"].get("deletionTimestamp")]
            assert len(live) == 2
            streams = []
            for replica in live:
                target = await stack.enter_async_context(_forward(cluster, f"pod/{replica['metadata']['name']}"))
                channel = await stack.enter_async_context(dial(target))
                reader = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
                stream = reader.WatchDispatches(
                    pb.WatchDispatchesRequest(filter=pb.DispatchFilter(session_id=deletion_session)),
                    metadata=authentication.metadata,
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
            for stream in streams:
                while True:
                    response = await stream.read()
                    if response == grpc.aio.EOF:
                        raise AssertionError("Replica watch missed the Sandbox deletion outcome")
                    assert isinstance(response, pb.WatchDispatchesResponse)
                    if response.HasField("dispatch") and response.dispatch.state == pb.DELIVERY_STATE_FAILED:
                        assert response.dispatch.reason == "sandbox_deleted"
                        assert response.dispatch.command_id == deletion_command
                        break
            evidence["deletion_observed_replicas"] = [item["metadata"]["uid"] for item in live]
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
    for name in (WORKLOAD, "sandbox-controller", "app", "notifications"):
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
    expected_wake_callers = [
        {"namespace": cluster.namespace, "name": "app"},
        {"namespace": cluster.namespace, "name": "notifications"},
        # The k3s fixture offers the orchestration_v1 group, whose sends wake Sandboxes too.
        {"namespace": cluster.namespace, "name": "actions"},
    ]
    accounts = {f"{account['namespace']}/{account['name']}" for account in expected_wake_callers}
    assert set(gateway_settings["dispatcher_callers"]) == accounts
    assert set(gateway_settings["wake_callers"]) == accounts

    service_config = json.loads(await cluster.kubectl("get", "configmap", "app-config", "-o", "json"))
    policies = yaml.safe_load(service_config["data"]["config.yaml"])["default_policy_sets"]
    assert isinstance(policies, list)
    assert policies
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"systest-wake-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": cluster.namespace,
        "images": deployments,
        "sandbox": sandbox,
        "session": session,
        "command": command,
        "wake_caller_accounts": expected_wake_callers,
        "passed": False,
        "policy_sets": policies,
    }
    path = undeclared_outputs_dir() / "dispatcher-wake.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    created = json.loads(
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "Sandbox",
                "metadata": {"name": sandbox, "namespace": cluster.namespace},
                "spec": {"template": POD_TEMPLATE, "running": False, "wakeOnSend": True, "policySets": policies},
            },
        )
    )
    uid = created["metadata"]["uid"]
    evidence["sandbox_uid"] = uid
    cleaned = False

    async def cleanup(error: BaseException | None = None) -> None:
        nonlocal cleaned
        if not cleaned:
            if isinstance(error, Exception):
                await capture_without_masking(
                    error, lambda: _capture_cleanup_diagnostics(crictl_command, cluster, sandbox, [session], [command])
                )
            await _cleanup_sandbox(crictl_command, cluster, sandbox, uid, session_ids=[session], command_ids=[command])
            cleaned = True

    try:
        await cluster.kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            "--for=jsonpath={.status.stopStage}=Stopped",
            "--timeout=300s",
        )
        stopped = json.loads(await cluster.kubectl("get", "sandbox", sandbox, "-o", "json"))
        initial_incarnation = stopped.get("status", {}).get("incarnation", {}).get("uid")
        evidence["initial_incarnation"] = initial_incarnation
        app_authentication, app_pod, app_pod_uid = await _bound_caller(cluster, "app")
        notification_authentication, notification_pod, notification_pod_uid = await _bound_caller(
            cluster, "notifications"
        )
        evidence["caller_pods"] = {
            "app": {"name": app_pod, "uid": app_pod_uid},
            "notifications": {"name": notification_pod, "uid": notification_pod_uid},
        }
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        async with _forward(cluster) as target:
            channel = dial(target)
            try:
                stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
                opened, queued = await asyncio.gather(
                    stub.OpenSession(
                        pb.OpenSessionRequest(
                            sandbox=sandbox,
                            session_id=session,
                            spec=runner_pb2.SessionSpec(
                                harness=runner_pb2.HARNESS_CODEX,
                                model="chatgpt-sub/oai-responses/gpt-6-luna",
                                cwd="/state/work",
                                reasoning_effort="low",
                                instructions=f"EARLY_DIAGNOSTIC_PAYLOAD_{sandbox}",
                            ),
                        ),
                        metadata=app_authentication.metadata,
                        timeout=15,
                    ),
                    stub.SubmitInput(
                        pb.SubmitInputRequest(
                            sandbox=sandbox,
                            sandbox_uid=uid,
                            session_id=session,
                            command_id=command,
                            text=f"LATE_DIAGNOSTIC_PAYLOAD_{sandbox} Reply with exactly OK and do not use tools.",
                        ),
                        metadata=notification_authentication.metadata,
                        timeout=15,
                    ),
                )
                assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
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
                    stub,
                    notification_authentication,
                    session,
                    lambda row: row.state == pb.DELIVERY_STATE_RUNNER_ADMITTED,
                )
                assert admitted.command_id == command
                assert admitted.admission.event.command_admitted.command.submit_input.text == (
                    f"LATE_DIAGNOSTIC_PAYLOAD_{sandbox} Reply with exactly OK and do not use tools."
                )
                deadline = asyncio.get_running_loop().time() + BOUND_SECONDS
                while True:
                    opening = await stub.GetSessionOpening(
                        pb.GetSessionOpeningRequest(session_id=session),
                        metadata=app_authentication.metadata,
                        timeout=15,
                    )
                    if opening.opening.state in (pb.DELIVERY_STATE_FAILED, pb.DELIVERY_STATE_UNKNOWN):
                        raise DispatchFailedError(
                            f"opening {pb.DeliveryState.Name(opening.opening.state)}: {opening.opening.reason}"
                        )
                    if opening.opening.state == pb.DELIVERY_STATE_RUNNER_ADMITTED:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise AssertionError("Session opening did not reach runner admission")
                    await asyncio.sleep(1)

                capture_id = uuid4().hex
                archive_dir = undeclared_outputs_dir() / f"dispatcher-data-proof-{sandbox}-{capture_id}"
                archive_dir.mkdir(parents=True)
                mount_sources = await _container_mount_sources(crictl_command, cluster, sandbox, archive_dir)
                state_session = mount_sources["/state"] / "sessions" / session
                early_payload = f"EARLY_ARCHIVE_PAYLOAD_{sandbox}".encode()
                late_payload = f"LATE_ARCHIVE_PAYLOAD_{sandbox}".encode()
                early_session_payload = f"EARLY_DIAGNOSTIC_PAYLOAD_{sandbox}".encode()
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
                    archive_manifest = await _archive_runner_data(
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
                        f"/sessions/{session}/session.json": early_session_payload,
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
                                    if suffix.endswith("session.json"):
                                        assert expected_payload in archived_payload
                                    else:
                                        assert archived_payload == expected_payload
                                    found_markers.add(suffix)
                    assert found_markers == set(expected)
                finally:
                    wal_db.close()
                evidence.update(
                    {
                        "ready_incarnation": ready_incarnation,
                        "opening_state": pb.DeliveryState.Name(opening.opening.state),
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
    finally:
        failure = sys.exception()
        await cleanup_without_masking(failure, lambda: cleanup(failure))
    evidence["cleanup_complete"] = True
    path.write_text(json.dumps(evidence, indent=2) + "\n")


# gazelle:include_dep @pypi//protobuf
