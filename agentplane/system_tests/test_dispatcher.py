"""Dispatcher queue/Ready system test, independent of the app's HTTP surface."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s, watch as k8s_watch

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import dial
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2 as pb, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.cleanup import cleanup_preserving_failure
from agentplane.system_tests.cluster import api_client, kubectl
from agentplane.system_tests.conftest import POD_TEMPLATE, namespace
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

SERVICE = "session-dispatcher"
WORKLOAD = "runner-gateway"
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


async def _delete_pod(name: str, uid: str) -> None:
    await _delete("pod", name, uid)


async def _bound_caller(service_account: str) -> tuple[Caller, str, str]:
    pods = json.loads(await kubectl("get", "pods", "-l", f"app.kubernetes.io/name={service_account}", "-o", "json"))
    pod = next(item for item in pods["items"] if not item["metadata"].get("deletionTimestamp"))
    name = pod["metadata"]["name"]
    uid = pod["metadata"]["uid"]
    token = await kubectl(
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


async def _delete(kind: str, name: str, uid: str) -> None:
    async with api_client() as api:
        options = k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid))
        if kind == "pod":
            await k8s.CoreV1Api(api).delete_namespaced_pod(name, namespace(), body=options)
        else:
            await k8s.CustomObjectsApi(api).delete_namespaced_custom_object(
                GROUP, VERSION, namespace(), "sandboxes", name, body=options
            )
    await kubectl("wait", f"{kind}/{name}", "--for=delete", "--timeout=120s")


async def _cleanup_sandbox(name: str, uid: str) -> None:
    await kubectl(
        "patch",
        "sandbox.agentplane.allegedly.works",
        name,
        "--type=merge",
        "-p",
        json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
    )
    await kubectl(
        "wait",
        f"sandbox.agentplane.allegedly.works/{name}",
        "--for=jsonpath={.status.operatingMode}=Suspended",
        "--timeout=300s",
    )
    await kubectl(
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
        namespace(),
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
    sandbox_uid: str, deployment_uid: str, evidence: dict[str, object]
) -> AsyncIterator[str]:
    pods = json.loads(await kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json"))
    (original,) = pods["items"]
    lease_name = f"dispatcher-{UUID(sandbox_uid).hex}"
    lease = json.loads(await kubectl("get", "lease", lease_name, "-o", "json"))
    holder = lease["spec"]["holderIdentity"]
    scaled_generation: int | None = None
    try:
        patched = json.loads(
            await kubectl(
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
        await kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")
        pods = json.loads(await kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json"))
        (peer,) = [item for item in pods["items"] if item["metadata"]["uid"] != original["metadata"]["uid"]]
        await _delete_pod(original["metadata"]["name"], original["metadata"]["uid"])
        async with api_client() as api, k8s_watch.Watch() as watch:
            coordination = k8s.CoordinationV1Api(api)
            current = await coordination.read_namespaced_lease(lease_name, namespace())
            if current.spec.holder_identity == holder:
                async with asyncio.timeout(BOUND_SECONDS):
                    async for event in watch.stream(
                        coordination.list_namespaced_lease,
                        namespace(),
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
            current = json.loads(await kubectl("get", "deployment", WORKLOAD, "-o", "json"))
            # Do not overwrite a concurrent operator or GitOps rollout.
            if current["metadata"]["uid"] == deployment_uid and current["metadata"]["generation"] == scaled_generation:
                await kubectl(
                    "scale",
                    f"deployment/{WORKLOAD}",
                    "--current-replicas=2",
                    "--replicas=1",
                    f"--resource-version={current['metadata']['resourceVersion']}",
                )
                await kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")


@pytest.mark.timeout(600)
@pytest.mark.parametrize("replica_failover", [False, True], ids=["queue", "replicas"])
async def test_deployed_dispatcher_waits_for_ready_then_records_runner_admission(replica_failover: bool) -> None:
    deployment = json.loads(await kubectl("get", "deployment", WORKLOAD, "-o", "json"))
    assert deployment["status"].get("readyReplicas") == 1, "the Dispatcher is not Ready"
    assert deployment["spec"]["replicas"] == 1
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    app_config = json.loads(await kubectl("get", "configmap", "app-config", "-o", "json"))
    policies = yaml.safe_load(app_config["data"]["config.yaml"])["default_policy_sets"]
    assert isinstance(policies, list)
    assert policies
    assert all(isinstance(name, str) and name for name in policies)
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"systest-dispatch-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": namespace(),
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
        authentication, caller, _caller_uid = await _bound_caller("app")
        created = json.loads(
            await kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": SANDBOX_API_VERSION,
                    "kind": "Sandbox",
                    "metadata": {"name": sandbox, "namespace": namespace()},
                    "spec": {"template": POD_TEMPLATE, "running": False, "policySets": policies},
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
        await kubectl(
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
        waiting_sandbox = json.loads(await kubectl("get", "sandbox", sandbox, "-o", "json"))
        assert waiting_sandbox["spec"].get("running") is False
        assert waiting_sandbox["spec"].get("wakeGeneration", 0) == 0
        evidence["waiting_reason"] = waiting.reason
        caller_view = json.loads(await kubectl("get", "pod", caller, "-o", "json"))
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
                    model="chatgpt-sub/oai-responses/gpt-6-luna",
                    cwd="/state/work",
                    reasoning_effort="low",
                ),
            ),
            metadata=authentication.metadata,
            timeout=15,
        )
        assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        await kubectl(
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
            await kubectl(
                "patch",
                "sandbox.agentplane.allegedly.works",
                sandbox,
                "--type=merge",
                "-p",
                json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
            )
            await kubectl(
                "wait",
                f"sandbox.agentplane.allegedly.works/{sandbox}",
                '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
                "--timeout=300s",
            )
            deletion_session = str(uuid4())
            deletion_command = str(uuid4())
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
            await kubectl("rollout", "status", f"deployment/{WORKLOAD}", "--timeout=120s")
            pods = json.loads(await kubectl("get", "pods", "-l", f"app.kubernetes.io/name={WORKLOAD}", "-o", "json"))
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


@pytest.mark.timeout(600)
async def test_deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends() -> None:
    deployments: dict[str, object] = {}
    for name in (WORKLOAD, "sandbox-controller", "app", "notifications"):
        deployment = json.loads(await kubectl("get", "deployment", name, "-o", "json"))
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
    gateway_config = json.loads(await kubectl("get", "configmap", f"{WORKLOAD}-settings", "-o", "json"))
    gateway_settings = yaml.safe_load(gateway_config["data"]["settings.yaml"])
    expected_wake_callers = [
        {"namespace": namespace(), "name": "app"},
        {"namespace": namespace(), "name": "notifications"},
        # The k3s fixture offers the orchestration_v1 group, whose sends wake Sandboxes too.
        {"namespace": namespace(), "name": "actions"},
    ]
    accounts = {f"{account['namespace']}/{account['name']}" for account in expected_wake_callers}
    assert set(gateway_settings["dispatcher_callers"]) == accounts
    assert set(gateway_settings["wake_callers"]) == accounts

    service_config = json.loads(await kubectl("get", "configmap", "app-config", "-o", "json"))
    policies = yaml.safe_load(service_config["data"]["config.yaml"])["default_policy_sets"]
    assert isinstance(policies, list)
    assert policies
    suffix = uuid4().hex[:8]
    sandbox, session, command = (f"systest-wake-{suffix}", str(uuid4()), str(uuid4()))
    evidence: dict[str, object] = {
        "namespace": namespace(),
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
        await kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "Sandbox",
                "metadata": {"name": sandbox, "namespace": namespace()},
                "spec": {"template": POD_TEMPLATE, "running": False, "wakeOnSend": True, "policySets": policies},
            },
        )
    )
    uid = created["metadata"]["uid"]
    evidence["sandbox_uid"] = uid
    cleaned = False

    async def cleanup() -> None:
        nonlocal cleaned
        if not cleaned:
            await _cleanup_sandbox(sandbox, uid)
            cleaned = True

    try:
        await kubectl(
            "wait",
            f"sandbox.agentplane.allegedly.works/{sandbox}",
            '--for=jsonpath={.status.conditions[?(@.type=="Ready")].reason}=Stopped',
            "--timeout=300s",
        )
        stopped = json.loads(await kubectl("get", "sandbox", sandbox, "-o", "json"))
        initial_incarnation = stopped.get("status", {}).get("incarnation", {}).get("uid")
        evidence["initial_incarnation"] = initial_incarnation
        app_authentication, app_pod, app_pod_uid = await _bound_caller("app")
        notification_authentication, notification_pod, notification_pod_uid = await _bound_caller("notifications")
        evidence["caller_pods"] = {
            "app": {"name": app_pod, "uid": app_pod_uid},
            "notifications": {"name": notification_pod, "uid": notification_pod_uid},
        }
        path.write_text(json.dumps(evidence, indent=2) + "\n")
        async with _forward() as target:
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
                            text="Reply with exactly OK and do not use tools.",
                        ),
                        metadata=notification_authentication.metadata,
                        timeout=15,
                    ),
                )
                assert opened.opening.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
                assert queued.dispatch.state == pb.DELIVERY_STATE_QUEUED_IN_DISPATCHER
                await kubectl(
                    "wait",
                    f"sandbox.agentplane.allegedly.works/{sandbox}",
                    "--for=jsonpath={.spec.wakeGeneration}=1",
                    "--timeout=120s",
                )
                waking = json.loads(await kubectl("get", "sandbox", sandbox, "-o", "json"))
                assert waking["spec"]["running"] is True
                assert waking["spec"]["wakeGeneration"] == 1
                evidence["wake_generation"] = waking["spec"]["wakeGeneration"]
                path.write_text(json.dumps(evidence, indent=2) + "\n")

                await kubectl(
                    "wait",
                    f"sandbox.agentplane.allegedly.works/{sandbox}",
                    "--for=condition=Ready=True",
                    "--timeout=300s",
                )
                ready = json.loads(await kubectl("get", "sandbox", sandbox, "-o", "json"))
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
                    "Reply with exactly OK and do not use tools."
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
                evidence.update(
                    {
                        "ready_incarnation": ready_incarnation,
                        "opening_state": pb.DeliveryState.Name(opening.opening.state),
                        "dispatch_state": pb.DeliveryState.Name(admitted.state),
                        "dispatch_admission_cursor": admitted.admission.cursor,
                        "passed": True,
                    }
                )
                path.write_text(json.dumps(evidence, indent=2) + "\n")
            finally:
                await channel.close()
    finally:
        await cleanup_preserving_failure(cleanup, sys.exception())
    evidence["cleanup_complete"] = True
    path.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    pytest_bazel.main()
