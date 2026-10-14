"""Testing-only Dispatcher queue/Ready acceptance, independent of the app's HTTP surface."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s, config as k8s_config, watch as k8s_watch

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc_calls import dial
from agentplane.proto import runner_pb2, session_dispatcher_pb2 as pb, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

NAMESPACE = "agentplane-testing"
SERVICE = "agentplane-session-dispatcher"
BOUND_SECONDS = 300
type Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub


@dataclass(frozen=True)
class Caller:
    token: str = field(repr=False)

    @property
    def metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {self.token}"),)


class DispatchFailedError(RuntimeError):
    pass


async def _kubectl(*args: str, body: dict[str, object] | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        *args,
        stdin=asyncio.subprocess.PIPE if body is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(BOUND_SECONDS + 10):
            stdout, stderr = await process.communicate(json.dumps(body).encode() if body is not None else None)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise RuntimeError(f"kubectl {' '.join(args)}: {stderr.decode()}")
    return stdout.decode().strip()


async def _delete_pod(name: str, uid: str) -> None:
    await _delete("pod", name, uid)


async def _delete(kind: str, name: str, uid: str) -> None:
    configuration = k8s.Configuration()
    await k8s_config.load_kube_config(client_configuration=configuration)
    async with k8s.ApiClient(configuration) as api:
        options = k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid))
        if kind == "pod":
            await k8s.CoreV1Api(api).delete_namespaced_pod(name, NAMESPACE, body=options)
        else:
            await k8s.CustomObjectsApi(api).delete_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, "sandboxes", name, body=options
            )
    await _kubectl("wait", f"{kind}/{name}", "--for=delete", "--timeout=120s")


async def _cleanup_sandbox(name: str, uid: str) -> None:
    await _kubectl(
        "patch",
        "sandbox.agentplane.allegedly.works",
        name,
        "--type=merge",
        "-p",
        json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
    )
    await _kubectl(
        "wait",
        f"sandbox.agentplane.allegedly.works/{name}",
        "--for=jsonpath={.status.operatingMode}=Suspended",
        "--timeout=300s",
    )
    await _kubectl(
        "wait",
        f"sandbox.agentplane.allegedly.works/{name}",
        '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
        "--timeout=300s",
    )
    await _delete("sandbox.agentplane.allegedly.works", name, uid)


@asynccontextmanager
async def _forward(resource: str = f"service/{SERVICE}") -> AsyncIterator[str]:
    port = pick_free_port()
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        "port-forward",
        resource,
        f"{port}:8080",
        "--address=127.0.0.1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    try:
        async with asyncio.timeout(30):
            while line := await process.stdout.readline():
                if line.startswith(b"Forwarding from 127.0.0.1:"):
                    break
            else:
                raise RuntimeError("Dispatcher port-forward ended before listening")
        yield f"127.0.0.1:{port}"
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


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
    sandbox_uid: str, deployment_uid: str, evidence: dict[str, object]
) -> AsyncIterator[str]:
    pods = json.loads(await _kubectl("get", "pods", "-l", f"app.kubernetes.io/name={SERVICE}", "-o", "json"))
    (original,) = pods["items"]
    lease_name = f"dispatcher-{UUID(sandbox_uid).hex}"
    lease = json.loads(await _kubectl("get", "lease", lease_name, "-o", "json"))
    holder = lease["spec"]["holderIdentity"]
    scaled_generation: int | None = None
    try:
        patched = json.loads(
            await _kubectl(
                "patch",
                "deployment",
                SERVICE,
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
        await _kubectl("rollout", "status", f"deployment/{SERVICE}", "--timeout=120s")
        pods = json.loads(await _kubectl("get", "pods", "-l", f"app.kubernetes.io/name={SERVICE}", "-o", "json"))
        (peer,) = [item for item in pods["items"] if item["metadata"]["uid"] != original["metadata"]["uid"]]
        await _delete_pod(original["metadata"]["name"], original["metadata"]["uid"])
        configuration = k8s.Configuration()
        await k8s_config.load_kube_config(client_configuration=configuration)
        async with k8s.ApiClient(configuration) as api, k8s_watch.Watch() as watch:
            coordination = k8s.CoordinationV1Api(api)
            current = await coordination.read_namespaced_lease(lease_name, NAMESPACE)
            if current.spec.holder_identity == holder:
                async with asyncio.timeout(BOUND_SECONDS):
                    async for event in watch.stream(
                        coordination.list_namespaced_lease,
                        NAMESPACE,
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
            current = json.loads(await _kubectl("get", "deployment", SERVICE, "-o", "json"))
            # Do not overwrite a concurrent operator or GitOps rollout.
            if current["metadata"]["uid"] == deployment_uid and current["metadata"]["generation"] == scaled_generation:
                await _kubectl(
                    "scale",
                    f"deployment/{SERVICE}",
                    "--current-replicas=2",
                    "--replicas=1",
                    f"--resource-version={current['metadata']['resourceVersion']}",
                )
                await _kubectl("rollout", "status", f"deployment/{SERVICE}", "--timeout=120s")


@pytest.mark.timeout(600)
@pytest.mark.parametrize("replica_failover", [False, True], ids=["queue", "replicas"])
async def test_deployed_dispatcher_waits_for_ready_then_records_runner_admission(replica_failover: bool) -> None:
    deployment = json.loads(await _kubectl("get", "deployment", SERVICE, "-o", "json"))
    assert deployment["status"].get("readyReplicas") == 1, "testing Dispatcher is not Ready"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    service_config = json.loads(await _kubectl("get", "configmap", "agentplane-sandbox-service-config", "-o", "json"))
    policies = yaml.safe_load(service_config["data"]["config.yaml"])["default_policy_sets"]
    assert isinstance(policies, list)
    assert policies
    assert all(isinstance(name, str) and name for name in policies)
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"accept-dispatch-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": NAMESPACE,
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
        callers = json.loads(await _kubectl("get", "pods", "-l", "app.kubernetes.io/name=agentplane-app", "-o", "json"))
        pod = next(item for item in callers["items"] if not item["metadata"].get("deletionTimestamp"))
        caller = pod["metadata"]["name"]
        caller_uid = pod["metadata"]["uid"]
        token = await _kubectl(
            "create",
            "token",
            "agentplane-app",
            f"--audience={SERVICE}",
            "--duration=600s",
            "--bound-object-kind=Pod",
            f"--bound-object-name={caller}",
            f"--bound-object-uid={caller_uid}",
        )
        authentication = Caller(token)
        created = json.loads(
            await _kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": sandbox, "namespace": NAMESPACE},
                    "spec": {"template": "runner", "running": False, "policySets": policies},
                },
            )
        )
        uid = created["metadata"]["uid"]
        evidence["sandbox_uid"] = uid
        cleaned = False

        async def cleanup() -> None:
            if not cleaned:
                await _cleanup_sandbox(sandbox, uid)

        stack.push_async_callback(cleanup)
        await _kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
            "--timeout=300s",
        )
        target = await stack.enter_async_context(_forward())
        channel = await stack.enter_async_context(dial(target))
        stub: Dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
            await stub.ListDispatches(pb.ListDispatchesRequest(filter=pb.DispatchFilter(sandbox=sandbox)), timeout=15)
        assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED
        queued = await stub.ChangeReasoningEffort(
            pb.ChangeReasoningEffortRequest(sandbox=sandbox, session_id=session, command_id=command, effort="low"),
            metadata=authentication.metadata,
            timeout=15,
        )
        assert queued.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        waiting = await _dispatch_until(stub, authentication, session, lambda row: row.reason == "sandbox_suspended")
        assert waiting.sandbox_uid == uid
        evidence["waiting_reason"] = waiting.reason
        caller_view = json.loads(await _kubectl("get", "pod", caller, "-o", "json"))
        evidence["caller_phase"] = caller_view.get("status", {}).get("phase")
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        if replica_failover:
            # Only one replica can have acquired this Sandbox's Lease before scaling.
            # Killing that known owner, then watching through the surviving peer, exercises
            # lease expiry and durable queue recovery without the app HTTP service.
            peer = await stack.enter_async_context(
                _replace_owner_with_peer(uid, deployment["metadata"]["uid"], evidence)
            )
            peer_target = await stack.enter_async_context(_forward(f"pod/{peer}"))
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
                    model="chatgpt/oai-responses/gpt-6-luna",
                    cwd="/state/work",
                    reasoning_effort="low",
                ),
            ),
            metadata=authentication.metadata,
            timeout=15,
        )
        assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        await _kubectl(
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
            await _kubectl(
                "patch",
                "sandbox.agentplane.allegedly.works",
                sandbox,
                "--type=merge",
                "-p",
                json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
            )
            await _kubectl(
                "wait",
                f"sandbox.agentplane.allegedly.works/{sandbox}",
                '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
                "--timeout=300s",
            )
            deletion_session = str(uuid4())
            deletion_command = str(uuid4())
            await stub.ChangeReasoningEffort(
                pb.ChangeReasoningEffortRequest(
                    sandbox=sandbox, session_id=deletion_session, command_id=deletion_command, effort="low"
                ),
                metadata=authentication.metadata,
                timeout=15,
            )
            await _kubectl("rollout", "status", f"deployment/{SERVICE}", "--timeout=120s")
            pods = json.loads(await _kubectl("get", "pods", "-l", f"app.kubernetes.io/name={SERVICE}", "-o", "json"))
            live = [item for item in pods["items"] if not item["metadata"].get("deletionTimestamp")]
            assert len(live) == 2
            streams = []
            for replica in live:
                target = await stack.enter_async_context(_forward(f"pod/{replica['metadata']['name']}"))
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
            await cleanup()
            cleaned = True
            for stream in streams:
                while True:
                    response = await stream.read()
                    if response == grpc.aio.EOF:
                        raise AssertionError("Replica watch missed the Sandbox deletion outcome")
                    assert isinstance(response, pb.WatchDispatchesResponse)
                    if response.dispatch.state == pb.DELIVERY_STATE_FAILED:
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


if __name__ == "__main__":
    pytest_bazel.main()
