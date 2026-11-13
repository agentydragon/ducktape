"""The deployed deletion fence, including a command already routed to the old Runtime."""

# gazelle:include_dep @pypi//protobuf

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
import yaml
from kubernetes_asyncio import client as k8s
from tenacity import AsyncRetrying, retry_if_exception, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.env.runtime.control import environment_runtime_pb2, environment_runtime_pb2_grpc
from agentplane.grpc.calls import dial
from agentplane.history_service.api.history import history_service_pb2, history_service_pb2_grpc
from agentplane.proto import session_pb2
from agentplane.runner_gateway import api
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.driver.cleanup_diagnostics import capture_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import Caller, bound_caller, forward
from agentplane.system_tests.driver.identity import (
    LAUNCH_DEFAULT_POLICY_SETS,
    ScenarioIdentity,
    create_scenario_identity,
    delete_scenario_identity,
    scenario_identity,
)
from agentplane.system_tests.driver.sandbox import (
    ENVIRONMENT_RUNTIME_CONTAINER,
    ENVIRONMENT_RUNTIME_LEASE_SUFFIX,
    POD_TEMPLATE,
)
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tls import service_credentials
from agentplane.system_tests.scenarios.dispatcher import container_mount_sources, session_until
from agentplane.testing.scripted_model import Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _retry_list_sessions_poll_error(error: BaseException) -> bool:
    """Retry an unsettled snapshot or a transient failure from the read-only poll."""
    return isinstance(error, AssertionError) or (
        isinstance(error, grpc.aio.AioRpcError) and error.code() == grpc.StatusCode.UNAVAILABLE
    )


async def _gateway_audience(cluster: Cluster) -> str:
    settings = json.loads(await cluster.kubectl("get", "configmap", "runner-gateway-settings", "-o", "json"))
    return cast(str, yaml.safe_load(settings["data"]["settings.yaml"])["gateway_audience"])


@asynccontextmanager
async def _sandbox(cluster: Cluster, name: str, policies: list[str]) -> AsyncIterator[str]:
    # A replacement Sandbox of the same name gets its own Identity.
    async with (
        scenario_identity(cluster, f"{name}-{uuid4().hex[:6]}", policies) as identity,
        _sandbox_as(cluster, name, identity) as uid,
    ):
        yield uid


@asynccontextmanager
async def _sandbox_as(cluster: Cluster, name: str, identity: str) -> AsyncIterator[str]:
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
                "spec": {"template": POD_TEMPLATE, "running": True, "identity": {"name": identity}},
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
async def _retain_quiesced_pod(cluster: Cluster, name: str, uid: str, artifact: Path) -> AsyncIterator[None]:
    """Keep the receiver alive after QUIESCED without changing controller or Runtime behavior."""
    policy = f"systest-retain-{uuid4().hex[:12]}"

    async def capture_policy() -> None:
        value = await cluster.kubectl("get", "validatingadmissionpolicy", policy, "-o", "json")
        (artifact / "retention-policy.json").write_text(value)

    async with cluster.api_client() as client:
        pods = k8s.CoreV1Api(client)
        # With a DeleteOptions body, the body owns dryRun; a query-only dryRun can delete the Pod.
        options = k8s.V1DeleteOptions(dry_run=["All"], preconditions=k8s.V1Preconditions(uid=uid))
        # Establish that the same dry-run delete succeeds before the admission policy exists.
        await pods.delete_namespaced_pod(name, cluster.namespace, body=options)
        assert (await pods.read_namespaced_pod(name, cluster.namespace)).metadata.uid == uid
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
                                "validations": [
                                    {"expression": f"oldObject.metadata.uid != '{uid}'", "reason": "Forbidden"}
                                ],
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
                        await pods.delete_namespaced_pod(name, cluster.namespace, body=options)
                    except k8s.ApiException as error:
                        if error.body is None:
                            raise
                        refusal = {"status": error.status, "body": json.loads(error.body)}
                    (artifact / "retention-dry-run.json").write_text(json.dumps(refusal, indent=2))
                    if refusal is not None and refusal["status"] != 403:
                        raise RuntimeError(f"Unexpected Pod retention result: {refusal}")
                    assert refusal is not None
                    break
            await capture_policy()
            checked = json.loads((artifact / "retention-policy.json").read_text())
            assert not checked.get("status", {}).get("typeChecking", {}).get("expressionWarnings", [])
            # An evaluation error must not turn the test's UID-specific hold into a blanket denial.
            (unrelated,) = (
                await pods.list_namespaced_pod(
                    cluster.namespace, label_selector="app.kubernetes.io/name=runner-gateway"
                )
            ).items
            await pods.delete_namespaced_pod(
                unrelated.metadata.name,
                cluster.namespace,
                body=k8s.V1DeleteOptions(
                    dry_run=["All"], preconditions=k8s.V1Preconditions(uid=unrelated.metadata.uid)
                ),
            )
            assert (
                await pods.read_namespaced_pod(unrelated.metadata.name, cluster.namespace)
            ).metadata.uid == unrelated.metadata.uid
            yield
        except Exception as error:
            await capture_without_masking(error, capture_policy)
            raise
        finally:
            await cluster.kubectl(
                "delete",
                f"validatingadmissionpolicybinding/{policy}",
                f"validatingadmissionpolicy/{policy}",
                "--ignore-not-found=true",
            )


async def _read_sealed_history(
    history: history_service_pb2_grpc.HistoryServiceAsyncStub, caller: Caller, session: str
) -> tuple[int, set[str], set[str]]:
    cursor = 0
    commands: set[str] = set()
    completed_turns: set[str] = set()
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
        completed_turns.update(
            entry.event.turn_completed.turn_id for entry in page.entries if entry.event.HasField("turn_completed")
        )
        if len(page.entries) < 1000:
            return page.last_cursor, commands, completed_turns
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


async def deployed_session_command_cannot_cross_deletion(
    crictl_command: tuple[str, ...], cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    artifact = undeclared_outputs_dir() / "session-deletion"
    artifact.mkdir()
    app, _, _ = await bound_caller(cluster, "app")
    audience = await _gateway_audience(cluster)
    gateway, _, _ = await bound_caller(cluster, "runner-gateway", audience=audience)
    name = f"systest-deletion-{uuid4().hex[:8]}"
    session, positive, delayed, observed, probe_session = (str(uuid4()) for _ in range(5))
    evidence: dict[str, object] = {"sandbox": name, "session": session, "delayed_command": delayed, "passed": False}

    def persist() -> None:
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(scripted.case_scope())
        uid = await stack.enter_async_context(_sandbox(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS)))
        spec = session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                environment_hosted=session_pb2.EnvironmentHostedPlacement(
                    environment=session_pb2.ExactEnvironment(name=name, uid=uid), harness=session_pb2.HARNESS_CODEX
                )
            ),
            model="chatgpt-sub/oai-responses/gpt-6-luna",
            cwd="/state",
            reasoning_effort="low",
        )
        server_credentials = await service_credentials(cluster)
        target = await stack.enter_async_context(forward(cluster))
        channel = await stack.enter_async_context(
            dial(
                target,
                channel_credentials=server_credentials,
                server_name=f"session-dispatcher.{cluster.namespace}.svc.cluster.local",
            )
        )
        history_settings = json.loads(await cluster.kubectl("get", "configmap", "history-service-config", "-o", "json"))
        history_config = yaml.safe_load(history_settings["data"]["config.yaml"])
        history_caller, _, _ = await bound_caller(cluster, "app", audience=history_config["token_audience"])
        history_target = await stack.enter_async_context(forward(cluster, "service/history-service"))
        history_channel = await stack.enter_async_context(
            dial(
                history_target,
                channel_credentials=server_credentials,
                server_name=f"history-service.{cluster.namespace}.svc.cluster.local",
            )
        )
        history: history_service_pb2_grpc.HistoryServiceAsyncStub = history_service_pb2_grpc.HistoryServiceStub(
            history_channel
        )
        dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub = (
            session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        )
        runner: environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub = (
            environment_runtime_pb2_grpc.EnvironmentRuntimeStub(channel)
        )
        metadata = _metadata(gateway, cluster, name, uid)
        await dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec),
            metadata=app.metadata,
            timeout=15,
        )
        await session_until(
            dispatcher,
            app,
            session,
            lambda row: (
                row.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
            ),
        )
        admitted = await runner.SubmitInput(
            environment_runtime_pb2.SubmitInputRequest(
                session_id=session, command_id=positive, text="Keep this test turn active until deletion."
            ),
            metadata=metadata,
            timeout=15,
        )
        assert admitted.admission.event.command_admitted.command.command_id == positive
        resource = json.loads(await cluster.kubectl("get", "sandbox", name, "-o", "json"))
        incarnation = resource["status"]["incarnation"]

        async def capture_runtime() -> None:
            async with asyncio.timeout(30):
                pod = await cluster.kubectl("get", "pod", incarnation["name"], "-o", "json")
                (artifact / "pod.json").write_text(pod)
                log = await cluster.kubectl("logs", incarnation["name"], "-c", ENVIRONMENT_RUNTIME_CONTAINER)
                (artifact / "runtime.log").write_text(log)

        model = scripted.model(incarnation["name"], session_pb2.HARNESS_CODEX)
        try:
            async with asyncio.timeout(120):
                await model.hold(await model.request())
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(30),
                wait=wait_fixed(0.1),
                retry=retry_if_exception(_retry_list_sessions_poll_error),
            ):
                with attempt:
                    snapshot = await runner.ListSessions(
                        session_pb2.ListSessionsRequest(), metadata=metadata, timeout=15
                    )
                    active = next(item for item in snapshot.sessions if item.session_id == session)
                    assert active.active_turn_id
                    turn_id = active.active_turn_id
        except Exception as error:
            await capture_without_masking(error, capture_runtime)
            raise
        evidence["active_turn_before_deletion"] = turn_id
        persist()

        sources = await container_mount_sources(crictl_command, cluster, incarnation["name"], artifact)
        gate = sources["/state"] / "deletion-test-admission"
        # The test Runtime creates this directory as its own UID before serving any requests.
        if not gate.is_dir():
            await capture_runtime()
            raise AssertionError("The Sandbox is not running the deletion-test Runtime")
        gate_stat = gate.stat()
        evidence["gate_owner"] = {"uid": gate_stat.st_uid, "gid": gate_stat.st_gid, "mode": oct(gate_stat.st_mode)}
        persist()
        (gate / "command").write_text(delayed)
        call = runner.InterruptTurn(
            environment_runtime_pb2.InterruptTurnRequest(session_id=session, command_id=delayed, turn_id=turn_id),
            metadata=metadata,
            timeout=180,
        )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(120), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
            ):
                with attempt:
                    if call.done():
                        await call
                        raise RuntimeError("Command completed before the admission gate was entered")
                    entered = gate / "entered"
                    assert await asyncio.to_thread(entered.exists), entered
                    assert await asyncio.to_thread(entered.read_text) == delayed
            assert not call.done()
            evidence.update({"sandbox_uid": uid, "pod_uid": incarnation["uid"], "routed_before_deletion": True})
            persist()
            async with _retain_quiesced_pod(cluster, incarnation["name"], incarnation["uid"], artifact):
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
                before = await _read_sealed_history(history, history_caller, session)
                assert positive in before[1]
                assert delayed not in before[1]
                assert turn_id in before[2]
                assert not call.done()
                evidence["quiesced_before_release"] = lease
                persist()
                (gate / "release").touch()
                with pytest.raises(grpc.aio.AioRpcError) as refused:
                    await call
                evidence["delayed_rpc_code"] = refused.value.code().name
                evidence["delayed_rpc_details"] = refused.value.details()
                result = gate / "result"
                evidence["runtime_handler_result"] = result.read_text() if result.exists() else None
                persist()
                assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION, refused.value
                assert evidence["runtime_handler_result"] == "FailedPrecondition", evidence
                assert refused.value.details() == "journal is sealed", evidence
                assert await _read_sealed_history(history, history_caller, session) == before
                evidence["sealed_cursor_unchanged"] = before[0]
                persist()
        except Exception as error:
            await capture_without_masking(error, capture_runtime)
            raise
        finally:
            (gate / "release").touch()
            call.cancel()
        # Removing the policy lets the unmodified controller finish deleting the old environment.
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=300s")
        await cluster.kubectl("wait", f"pod/{incarnation['name']}", "--for=delete", "--timeout=120s")
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                retained = await dispatcher.GetSession(
                    session_dispatcher_pb2.GetSessionRequest(session_id=session), metadata=app.metadata, timeout=15
                )
                assert (
                    retained.session.status.required_environment
                    == session_dispatcher_pb2.SessionStatus.REQUIRED_ENVIRONMENT_DELETED
                )
        assert (
            retained.session.status.materialization
            == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
        )
        assert retained.session.placement.environment_hosted.environment.uid == uid
        retry = await dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec),
            metadata=app.metadata,
            timeout=15,
        )
        assert retry.session == retained.session
        evidence["retained_session_after_environment_deletion"] = True
        persist()
        replacement = await stack.enter_async_context(_sandbox(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS)))
        assert replacement != uid
        replacement_session = str(uuid4())
        replacement_spec = session_pb2.SessionSpec()
        replacement_spec.CopyFrom(spec)
        replacement_spec.placement.environment_hosted.environment.uid = replacement
        # A replacement Environment can host a new Session, but cannot adopt the old one.
        await dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(session_id=replacement_session, spec=replacement_spec),
            metadata=app.metadata,
            timeout=15,
        )
        replacement_metadata = _metadata(gateway, cluster, name, replacement)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception(_retry_list_sessions_poll_error)
        ):
            with attempt:
                sessions = await runner.ListSessions(
                    session_pb2.ListSessionsRequest(), metadata=replacement_metadata, timeout=15
                )
                assert replacement_session in {item.session_id for item in sessions.sessions}
        replacement_admitted = await runner.ChangeReasoningEffort(
            environment_runtime_pb2.ChangeReasoningEffortRequest(
                session_id=replacement_session, command_id=str(uuid4()), effort="low"
            ),
            metadata=replacement_metadata,
            timeout=15,
        )
        assert replacement_admitted.admission.event.HasField("command_admitted")
        # This RPC has no Session ID: NOT_FOUND proves UID routing refused the request, even if
        # an installation retains old Sessions across a name replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale_read:
            await runner.ListSessions(session_pb2.ListSessionsRequest(), metadata=metadata, timeout=15)
        assert stale_read.value.code() == grpc.StatusCode.NOT_FOUND
        # Preserve the delayed call's payload and old UID; it must not reach this healthy replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale:
            await runner.InterruptTurn(
                environment_runtime_pb2.InterruptTurnRequest(session_id=session, command_id=delayed, turn_id=turn_id),
                metadata=metadata,
                timeout=15,
            )
        assert stale.value.code() == grpc.StatusCode.NOT_FOUND
        evidence.update({"replacement_uid": replacement, "stale_uid_result": "NOT_FOUND", "passed": True})
        persist()


async def _wait_identity_and_binding_deleted(cluster: Cluster, identity: ScenarioIdentity) -> None:
    async def wait_absent(kind: str, name: str) -> None:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                current = await cluster.kubectl("get", kind, name, "--ignore-not-found", "-o", "json")
                assert not current, f"{kind}/{name} still exists: {current}"

    await wait_absent("identity", identity["name"])
    await wait_absent("policybinding", f"{identity['name']}.identity")


def _app_pod_selector(deployment: dict[str, Any]) -> str:
    match_labels = deployment["spec"]["selector"].get("matchLabels", {})
    assert match_labels, deployment["spec"]["selector"]
    return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))


async def _wait_app_replicas(cluster: Cluster, expected: int) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            deployment = json.loads(await cluster.kubectl("get", "deployment", "app", "-o", "json"))
            ready = deployment.get("status", {}).get("readyReplicas", 0)
            pods = json.loads(await cluster.kubectl("get", "pods", "-l", _app_pod_selector(deployment), "-o", "json"))[
                "items"
            ]
            assert ready == expected, (ready, expected)
            assert len(pods) == expected, (pods, expected)


async def _wait_app_down(cluster: Cluster) -> None:
    await _wait_app_replicas(cluster, 0)


async def _wait_retained_turn(
    history: history_service_pb2_grpc.HistoryServiceAsyncStub, caller: Caller, session: str
) -> int:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            try:
                page = await history.ReadSessionEvents(
                    history_service_pb2.ReadSessionEventsRequest(session_id=session, after_cursor=0, limit=1000),
                    metadata=caller.metadata,
                    timeout=15,
                )
            except grpc.aio.AioRpcError as error:
                if error.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNAVAILABLE):
                    raise AssertionError(f"History has not retained Session {session} yet") from error
                raise
            assert page.entries, page
            has_completed_turn = any(entry.event.HasField("turn_completed") for entry in page.entries)
            assert has_completed_turn, page
            return page.last_cursor
    raise AssertionError(f"History did not retain a completed turn for Session {session}")


async def _create_retained_turn(
    *,
    cluster: Cluster,
    scripted: ScriptedUpstream,
    app: Caller,
    gateway: Caller,
    history: history_service_pb2_grpc.HistoryServiceAsyncStub,
    history_reader: Caller,
    dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub,
    runner: environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub,
    sandbox: str,
    sandbox_uid: str,
    pod: str,
    marker: str,
) -> str:
    session = str(uuid4())
    spec = session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=sandbox, uid=sandbox_uid),
                harness=session_pb2.HARNESS_CODEX,
            )
        ),
        model="chatgpt-sub/oai-responses/gpt-6-luna",
        cwd="/state",
        reasoning_effort="low",
    )
    await dispatcher.CreateSession(
        session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec), metadata=app.metadata, timeout=15
    )
    await session_until(
        dispatcher,
        app,
        session,
        lambda row: row.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED,
    )
    metadata = _metadata(gateway, cluster, sandbox, sandbox_uid)
    admitted = await runner.SubmitInput(
        environment_runtime_pb2.SubmitInputRequest(session_id=session, command_id=str(uuid4()), text=marker),
        metadata=metadata,
        timeout=15,
    )
    assert admitted.admission.event.HasField("command_admitted"), admitted
    model = scripted.model(pod, session_pb2.HARNESS_CODEX)
    await model.reply(await model.request(), Text(marker))
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception(_retry_list_sessions_poll_error)
    ):
        with attempt:
            snapshot = await runner.ListSessions(session_pb2.ListSessionsRequest(), metadata=metadata, timeout=15)
            row = next(item for item in snapshot.sessions if item.session_id == session)
            assert not row.active_turn_id, row
    await _wait_retained_turn(history, history_reader, session)
    return session


async def managed_cli_reads_retained_history_with_identity_uid_scope(
    cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    """The mounted CLI reads its own retained Session with app down and cannot cross Identity UIDs."""
    artifact = undeclared_outputs_dir() / f"managed-cli-history-{uuid4().hex[:8]}"
    artifact.mkdir()
    evidence: dict[str, object] = {"passed": False}

    def persist() -> None:
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    async with scripted.case_scope():
        app, _, _ = await bound_caller(cluster, "app")
        gateway_audience = await _gateway_audience(cluster)
        gateway, _, _ = await bound_caller(cluster, "runner-gateway", audience=gateway_audience)
        history_settings = json.loads(await cluster.kubectl("get", "configmap", "history-service-config", "-o", "json"))
        history_config = yaml.safe_load(history_settings["data"]["config.yaml"])
        history_reader, _, _ = await bound_caller(cluster, "app", audience=history_config["token_audience"])
        server_credentials = await service_credentials(cluster)
        async with (
            forward(cluster) as dispatcher_target,
            forward(cluster, "service/history-service") as history_target,
            AsyncExitStack() as identities,
        ):
            dispatcher_channel = await identities.enter_async_context(
                dial(
                    dispatcher_target,
                    channel_credentials=server_credentials,
                    server_name=f"session-dispatcher.{cluster.namespace}.svc.cluster.local",
                )
            )
            history_channel = await identities.enter_async_context(
                dial(
                    history_target,
                    channel_credentials=server_credentials,
                    server_name=f"history-service.{cluster.namespace}.svc.cluster.local",
                )
            )
            dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(dispatcher_channel)
            runner = environment_runtime_pb2_grpc.EnvironmentRuntimeStub(dispatcher_channel)
            history: history_service_pb2_grpc.HistoryServiceAsyncStub = history_service_pb2_grpc.HistoryServiceStub(
                history_channel
            )

            shared_name = f"systest-history-{uuid4().hex[:8]}"
            other_name = f"{shared_name}-other"
            old_identity = await create_scenario_identity(cluster, shared_name, list(LAUNCH_DEFAULT_POLICY_SETS))
            identities.push_async_callback(delete_scenario_identity, cluster, old_identity)
            async with _sandbox_as(cluster, shared_name, old_identity["name"]) as old_uid:
                old_pod = json.loads(await cluster.kubectl("get", "sandbox", shared_name, "-o", "json"))["status"][
                    "incarnation"
                ]["name"]
                old_session = await _create_retained_turn(
                    cluster=cluster,
                    scripted=scripted,
                    app=app,
                    gateway=gateway,
                    history=history,
                    history_reader=history_reader,
                    dispatcher=dispatcher,
                    runner=runner,
                    sandbox=shared_name,
                    sandbox_uid=old_uid,
                    pod=old_pod,
                    marker=f"old-identity-{uuid4().hex}",
                )
            old_cursor = await _wait_retained_turn(history, history_reader, old_session)
            await delete_scenario_identity(cluster, old_identity)
            await _wait_identity_and_binding_deleted(cluster, old_identity)
            old_cursor_after_identity_delete = await _wait_retained_turn(history, history_reader, old_session)

            other_identity = await create_scenario_identity(cluster, other_name, list(LAUNCH_DEFAULT_POLICY_SETS))
            identities.push_async_callback(delete_scenario_identity, cluster, other_identity)
            async with AsyncExitStack() as sandboxes:
                other_uid = await sandboxes.enter_async_context(
                    _sandbox_as(cluster, other_name, other_identity["name"])
                )
                other_pod = json.loads(await cluster.kubectl("get", "sandbox", other_name, "-o", "json"))["status"][
                    "incarnation"
                ]["name"]
                other_marker = f"other-identity-{uuid4().hex}"
                other_session = await _create_retained_turn(
                    cluster=cluster,
                    scripted=scripted,
                    app=app,
                    gateway=gateway,
                    history=history,
                    history_reader=history_reader,
                    dispatcher=dispatcher,
                    runner=runner,
                    sandbox=other_name,
                    sandbox_uid=other_uid,
                    pod=other_pod,
                    marker=other_marker,
                )

                current_identity = await create_scenario_identity(
                    cluster, shared_name, list(LAUNCH_DEFAULT_POLICY_SETS)
                )
                identities.push_async_callback(delete_scenario_identity, cluster, current_identity)
                assert current_identity["uid"] != old_identity["uid"]
                current_uid = await sandboxes.enter_async_context(
                    _sandbox_as(cluster, shared_name, current_identity["name"])
                )
                current_pod = json.loads(await cluster.kubectl("get", "sandbox", shared_name, "-o", "json"))["status"][
                    "incarnation"
                ]["name"]
                current_marker = f"current-identity-{uuid4().hex}"
                current_session = await _create_retained_turn(
                    cluster=cluster,
                    scripted=scripted,
                    app=app,
                    gateway=gateway,
                    history=history,
                    history_reader=history_reader,
                    dispatcher=dispatcher,
                    runner=runner,
                    sandbox=shared_name,
                    sandbox_uid=current_uid,
                    pod=current_pod,
                    marker=current_marker,
                )
                evidence.update(
                    {
                        "identity_name": shared_name,
                        "old_identity_uid": old_identity["uid"],
                        "current_identity_uid": current_identity["uid"],
                        "other_identity_uid": other_identity["uid"],
                        "old_session": old_session,
                        "old_session_history_cursor_before_identity_delete": old_cursor,
                        "old_session_history_cursor_after_identity_delete": old_cursor_after_identity_delete,
                        "other_session": other_session,
                        "current_session": current_session,
                    }
                )
                persist()

                app_deployment = json.loads(await cluster.kubectl("get", "deployment", "app", "-o", "json"))
                original_replicas = app_deployment.get("spec", {}).get("replicas", 1)
                try:
                    await cluster.kubectl("scale", "deployment/app", "--replicas=0")
                    await _wait_app_down(cluster)

                    async def run_cli(*args: str) -> dict[str, Any]:
                        output = await cluster.kubectl(
                            "exec",
                            current_pod,
                            "-c",
                            ENVIRONMENT_RUNTIME_CONTAINER,
                            "--",
                            "/run/agentplane/environment/bin/agentplane",
                            "--json",
                            *args,
                        )
                        page: dict[str, Any] = json.loads(output)
                        return page

                    snapshot = await run_cli("sessions", "get", current_session)
                    assert snapshot.get("session_id") == current_session, snapshot
                    assert snapshot.get("session", {}).get("source") == "dispatcher", snapshot
                    assert snapshot.get("session", {}).get("kind") == "observed", snapshot
                    assert snapshot.get("history", {}).get("source") == "history", snapshot
                    assert snapshot.get("history", {}).get("kind") == "observed", snapshot

                    own_page = await run_cli("sessions", "read-turns", current_session, "--count", "1")
                    own_turns = own_page.get("turns", [])
                    assert len(own_turns) == 1, own_page
                    assert own_turns[0].get("final_assistant_message") == current_marker, own_page
                    assert own_page.get("error") is None, own_page
                    other_page = await run_cli("sessions", "read-turns", other_session, "--count", "1")
                    old_page = await run_cli("sessions", "read-turns", old_session, "--count", "1")
                    for target_session, page in ((other_session, other_page), (old_session, old_page)):
                        assert page.get("source") == "history", page
                        assert page.get("error", {}).get("code") == "NotFound", (target_session, page)
                        assert page.get("turns") == [], (target_session, page)
                    evidence.update(
                        {
                            "app_ready_replicas_after_scale_down": 0,
                            "current_identity_session_get": snapshot,
                            "current_identity_own_read": own_page,
                            "other_identity_read_refusal": other_page,
                            "old_identity_uid_read_refusal": old_page,
                            "passed": True,
                        }
                    )
                    persist()
                finally:
                    await cluster.kubectl("scale", "deployment/app", f"--replicas={original_replicas}")
                    await _wait_app_replicas(cluster, original_replicas)
