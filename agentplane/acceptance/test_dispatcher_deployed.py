"""Testing-only Dispatcher queue/Ready acceptance, independent of the app's HTTP surface."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

import grpc
import pytest
import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s, config as k8s_config

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
async def _forward() -> AsyncIterator[str]:
    port = pick_free_port()
    process = await asyncio.create_subprocess_exec(
        "kubectl",
        "-n",
        NAMESPACE,
        "port-forward",
        f"service/{SERVICE}",
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


@pytest.mark.timeout(600)
async def test_deployed_dispatcher_waits_for_ready_then_records_runner_admission() -> None:
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
    sandbox, caller, session, command = (
        f"accept-dispatch-{suffix}",
        f"accept-dispatch-caller-{suffix}",
        str(uuid4()),
        str(uuid4()),
    )
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
        pod = json.loads(
            await _kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                body={
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": caller, "namespace": NAMESPACE},
                    "spec": {
                        "serviceAccountName": "agentplane-app",
                        "imagePullSecrets": deployment["spec"]["template"]["spec"].get("imagePullSecrets", []),
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "caller",
                                "image": image,
                                "command": ["/bin/sleep", "infinity"],
                                "resources": {
                                    "requests": {"cpu": "10m", "memory": "16Mi"},
                                    "limits": {"cpu": "100m", "memory": "64Mi"},
                                },
                            }
                        ],
                    },
                },
            )
        )
        caller_uid = pod["metadata"]["uid"]
        stack.push_async_callback(_delete_pod, caller, caller_uid)
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
        stack.push_async_callback(_cleanup_sandbox, sandbox, uid)
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
        # Opening blocks the Session's commands. Queue it after observing the command's own
        # suspended reason; it must still reach the runner before the setting change.
        opened = await stub.OpenSession(
            pb.OpenSessionRequest(
                sandbox=sandbox,
                session_id=session,
                spec=runner_pb2.SessionSpec(
                    harness=runner_pb2.HARNESS_CODEX, model="chatgpt/oai-responses/gpt-6-luna", cwd="/workspace"
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
        except DispatchFailedError as error:
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
