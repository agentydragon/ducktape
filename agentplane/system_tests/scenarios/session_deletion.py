"""The deployed deletion fence, including a command already routed to the old Runtime."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import yaml
from kubernetes_asyncio import client as k8s
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.env.runtime.control import environment_runtime_pb2, environment_runtime_pb2_grpc
from agentplane.grpc.calls import dial
from agentplane.history_service import history_service_pb2, history_service_pb2_grpc
from agentplane.proto import session_pb2
from agentplane.runner_gateway import api
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.dispatcher_transport import Caller, bound_caller, forward
from agentplane.system_tests.sandbox import ENVIRONMENT_RUNTIME_LEASE_SUFFIX, POD_TEMPLATE
from agentplane.system_tests.scenarios.dispatcher import container_mount_sources
from util.testing.undeclared_outputs import undeclared_outputs_dir


@asynccontextmanager
async def _sandbox(cluster: Cluster, name: str, policies: list[str]) -> AsyncIterator[str]:
    resource = json.loads(
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "Sandbox",
                "metadata": {"name": name, "namespace": cluster.namespace},
                "spec": {"template": POD_TEMPLATE, "running": True, "policySets": policies},
            },
        )
    )
    uid = resource["metadata"]["uid"]
    try:
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=condition=Ready=True", "--timeout=300s")
        yield uid
    finally:
        async with cluster.api_client() as client:
            objects = k8s.CustomObjectsApi(client)
            try:
                current = await objects.get_namespaced_custom_object(
                    GROUP, VERSION, cluster.namespace, "sandboxes", name
                )
            except k8s.ApiException as error:
                if error.status != 404:
                    raise
            else:
                if current["metadata"]["uid"] == uid:
                    await objects.delete_namespaced_custom_object(
                        GROUP,
                        VERSION,
                        cluster.namespace,
                        "sandboxes",
                        name,
                        body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
                    )
                    await cluster.kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=300s")


@asynccontextmanager
async def _retain_quiesced_pod(cluster: Cluster, name: str, uid: str) -> AsyncIterator[None]:
    """Keep the receiver alive after QUIESCED without changing controller or Runtime behavior."""
    policy = f"systest-retain-{uuid4().hex[:12]}"
    async with cluster.api_client() as client:
        pods = k8s.CoreV1Api(client)
        options = k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid))
        # Establish that the same dry-run delete succeeds before the admission policy exists.
        await pods.delete_namespaced_pod(name, cluster.namespace, body=options, dry_run="All")
        try:
            await cluster.kubectl(
                "create",
                "-f",
                "-",
                body={
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": [
                        {
                            "apiVersion": "admissionregistration.k8s.io/v1",
                            "kind": "ValidatingAdmissionPolicy",
                            "metadata": {"name": policy},
                            "spec": {
                                "failurePolicy": "Fail",
                                "matchConstraints": {
                                    "resourceRules": [
                                        {
                                            "apiGroups": [""],
                                            "apiVersions": ["v1"],
                                            "operations": ["DELETE"],
                                            "resources": ["pods"],
                                        }
                                    ]
                                },
                                "validations": [{"expression": f"oldObject.metadata.uid != '{uid}'"}],
                            },
                        },
                        {
                            "apiVersion": "admissionregistration.k8s.io/v1",
                            "kind": "ValidatingAdmissionPolicyBinding",
                            "metadata": {"name": policy},
                            "spec": {"policyName": policy, "validationActions": ["Deny"]},
                        },
                    ],
                },
            )
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    refusal = None
                    try:
                        await pods.delete_namespaced_pod(name, cluster.namespace, body=options, dry_run="All")
                    except k8s.ApiException as error:
                        refusal = error.status
                    assert refusal == 403
                    break
            yield
        finally:
            await cluster.kubectl(
                "delete",
                f"validatingadmissionpolicybinding/{policy}",
                f"validatingadmissionpolicy/{policy}",
                "--ignore-not-found=true",
            )


async def _file(path: Path, expected: str) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            assert await asyncio.to_thread(path.exists), path
            assert await asyncio.to_thread(path.read_text) == expected


async def _history(
    history: history_service_pb2_grpc.HistoryServiceAsyncStub, caller: Caller, session: str
) -> tuple[int, set[str]]:
    cursor = 0
    commands: set[str] = set()
    while True:
        page = await history.ReadSessionEvents(
            history_service_pb2.ReadSessionEventsRequest(session_id=session, after_cursor=cursor, limit=1000),
            metadata=caller.metadata,
            timeout=15,
        )
        assert page.feed_state.HasField("sealed")
        assert page.feed_state.sealed.cursor == page.last_cursor
        commands.update(
            entry.event.command_admitted.command.command_id
            for entry in page.entries
            if entry.event.HasField("command_admitted")
        )
        if len(page.entries) < 1000:
            return page.last_cursor, commands
        cursor = page.entries[-1].cursor


def _metadata(caller: Caller, cluster: Cluster, name: str, uid: str) -> tuple[tuple[str, str], ...]:
    return (
        *caller.metadata,
        (api.SANDBOX_HEADER, name),
        (api.SANDBOX_NAMESPACE_HEADER, cluster.namespace),
        (api.SANDBOX_UID_HEADER, uid),
        (api.SANDBOX_OWNER_NAMESPACE_HEADER, cluster.namespace),
        (api.SANDBOX_OWNER_NAME_HEADER, name),
    )


async def deployed_session_command_cannot_cross_deletion(crictl_command: tuple[str, ...], cluster: Cluster) -> None:
    artifact = undeclared_outputs_dir() / "session-deletion"
    artifact.mkdir()
    settings = json.loads(await cluster.kubectl("get", "configmap", "runner-gateway-settings", "-o", "json"))
    config = yaml.safe_load(settings["data"]["settings.yaml"])
    app, _, _ = await bound_caller(cluster, "app")
    gateway, _, _ = await bound_caller(cluster, "runner-gateway", audience=config["gateway_audience"])
    name = f"systest-deletion-{uuid4().hex[:8]}"
    session, positive, delayed, observed, probe_session = (str(uuid4()) for _ in range(5))
    spec = session_pb2.SessionSpec(
        harness=session_pb2.HARNESS_CODEX,
        model="chatgpt-sub/oai-responses/gpt-6-luna",
        cwd="/state/work",
        reasoning_effort="low",
    )
    evidence: dict[str, object] = {"sandbox": name, "session": session, "delayed_command": delayed, "passed": False}

    def persist() -> None:
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    async with AsyncExitStack() as stack:
        uid = await stack.enter_async_context(_sandbox(cluster, name, config["environment_default_policy_sets"]))
        target = await stack.enter_async_context(forward(cluster))
        channel = await stack.enter_async_context(dial(target))
        history_settings = json.loads(await cluster.kubectl("get", "configmap", "history-service-config", "-o", "json"))
        history_config = yaml.safe_load(history_settings["data"]["config.yaml"])
        history_caller, _, _ = await bound_caller(cluster, "app", audience=history_config["token_audience"])
        history_target = await stack.enter_async_context(forward(cluster, "service/history-service"))
        history_channel = await stack.enter_async_context(dial(history_target))
        history: history_service_pb2_grpc.HistoryServiceAsyncStub = history_service_pb2_grpc.HistoryServiceStub(
            history_channel
        )
        dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        runner = environment_runtime_pb2_grpc.EnvironmentRuntimeStub(channel)
        metadata = _metadata(gateway, cluster, name, uid)
        await dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(sandbox=name, sandbox_uid=uid, session_id=session, spec=spec),
            metadata=app.metadata,
            timeout=15,
        )
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                opening = await dispatcher.GetSessionOpening(
                    session_dispatcher_pb2.GetSessionOpeningRequest(session_id=session),
                    metadata=app.metadata,
                    timeout=15,
                )
                assert opening.opening.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, opening
        admitted = await runner.ChangeReasoningEffort(
            environment_runtime_pb2.ChangeReasoningEffortRequest(session_id=session, command_id=positive, effort="low"),
            metadata=metadata,
            timeout=15,
        )
        assert admitted.admission.event.command_admitted.command.command_id == positive
        resource = json.loads(await cluster.kubectl("get", "sandbox", name, "-o", "json"))
        incarnation = resource["status"]["incarnation"]
        sources = await container_mount_sources(crictl_command, cluster, incarnation["name"], artifact)
        gate = sources["/state"] / "deletion-test-admission"
        gate.mkdir()
        owner = sources["/state"].stat()
        os.chown(gate, owner.st_uid, owner.st_gid)
        (gate / "command").write_text(delayed)
        call = runner.ChangeReasoningEffort(
            environment_runtime_pb2.ChangeReasoningEffortRequest(session_id=session, command_id=delayed, effort="high"),
            metadata=metadata,
            timeout=180,
        )
        try:
            await _file(gate / "entered", delayed)
            assert not call.done()
            evidence.update({"sandbox_uid": uid, "pod_uid": incarnation["uid"], "routed_before_deletion": True})
            persist()
            async with _retain_quiesced_pod(cluster, incarnation["name"], incarnation["uid"]):
                async with cluster.api_client() as client:
                    deleting = await k8s.CustomObjectsApi(client).delete_namespaced_custom_object(
                        GROUP,
                        VERSION,
                        cluster.namespace,
                        "sandboxes",
                        name,
                        body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
                    )
                assert deleting["metadata"]["deletionTimestamp"]
                evidence["deletion_timestamp"] = deleting["metadata"]["deletionTimestamp"]
                # A new mutation proves this Gateway observed deletion while the earlier call stays held.
                async for attempt in AsyncRetrying(
                    stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
                ):
                    with attempt:
                        with pytest.raises(grpc.aio.AioRpcError) as refused:
                            await runner.ChangeReasoningEffort(
                                environment_runtime_pb2.ChangeReasoningEffortRequest(
                                    session_id=probe_session, command_id=observed, effort="low"
                                ),
                                metadata=metadata,
                                timeout=15,
                            )
                        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                evidence["gateway_observed_deletion"] = True
                lease_name = name + ENVIRONMENT_RUNTIME_LEASE_SUFFIX
                await cluster.kubectl(
                    "wait",
                    f"lease/{lease_name}",
                    "--for=jsonpath={.metadata.annotations.agentplane\\.allegedly\\.works/quiesce-stage}=quiesced",
                    "--timeout=90s",
                )
                lease = json.loads(await cluster.kubectl("get", "lease", lease_name, "-o", "json"))
                assert lease["metadata"]["annotations"]["agentplane.allegedly.works/pod-uid"] == incarnation["uid"]
                pod = json.loads(await cluster.kubectl("get", "pod", incarnation["name"], "-o", "json"))
                assert pod["metadata"]["uid"] == incarnation["uid"]
                assert not pod["metadata"].get("deletionTimestamp")
                before = await _history(history, history_caller, session)
                assert positive in before[1]
                assert delayed not in before[1]
                assert observed not in before[1]
                assert not call.done()
                evidence["quiesced_before_release"] = lease
                persist()
                (gate / "release").touch()
                with pytest.raises(grpc.aio.AioRpcError) as refused:
                    await call
                assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                await _file(gate / "result", "FailedPrecondition")
                assert await _history(history, history_caller, session) == before
                evidence["runtime_handler_result"] = "FailedPrecondition"
                evidence["sealed_cursor_unchanged"] = before[0]
                persist()
        finally:
            (gate / "release").touch()
            call.cancel()
        # Removing the policy lets the unmodified controller finish deleting the old environment.
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=300s")
        await cluster.kubectl("wait", f"pod/{incarnation['name']}", "--for=delete", "--timeout=120s")
        replacement = await stack.enter_async_context(
            _sandbox(cluster, name, config["environment_default_policy_sets"])
        )
        assert replacement != uid
        replacement_session = str(uuid4())
        # Use a fresh Session ID: the test imposes no retention policy on old Session UUIDs.
        await dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(
                sandbox=name, sandbox_uid=replacement, session_id=replacement_session, spec=spec
            ),
            metadata=app.metadata,
            timeout=15,
        )
        replacement_metadata = _metadata(gateway, cluster, name, replacement)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                sessions = await runner.ListSessions(
                    session_pb2.ListSessionsRequest(), metadata=replacement_metadata, timeout=15
                )
                assert replacement_session in {item.session_id for item in sessions.sessions}
        admitted = await runner.ChangeReasoningEffort(
            environment_runtime_pb2.ChangeReasoningEffortRequest(
                session_id=replacement_session, command_id=str(uuid4()), effort="low"
            ),
            metadata=replacement_metadata,
            timeout=15,
        )
        assert admitted.admission.event.HasField("command_admitted")
        # This RPC has no Session ID: NOT_FOUND proves UID routing refused the request, even if
        # an installation retains old Sessions across a name replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale_read:
            await runner.ListSessions(session_pb2.ListSessionsRequest(), metadata=metadata, timeout=15)
        assert stale_read.value.code() == grpc.StatusCode.NOT_FOUND
        # Preserve the delayed call's payload and old UID; it must not reach this healthy replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale:
            await runner.ChangeReasoningEffort(
                environment_runtime_pb2.ChangeReasoningEffortRequest(
                    session_id=session, command_id=delayed, effort="high"
                ),
                metadata=metadata,
                timeout=15,
            )
        assert stale.value.code() == grpc.StatusCode.NOT_FOUND
        evidence.update({"replacement_uid": replacement, "stale_uid_result": "NOT_FOUND", "passed": True})
        persist()
