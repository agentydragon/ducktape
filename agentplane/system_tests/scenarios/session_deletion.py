"""The deployed deletion fence, including a command already routed to the old Runtime."""

# gazelle:include_dep @pypi//protobuf

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
from kubernetes_asyncio import client as k8s
from tenacity import AsyncRetrying, retry_if_exception, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP, VERSION
from agentplane.grpc.calls import dial
from agentplane.harness_host import harness_host_pb2, harness_host_pb2_grpc
from agentplane.history_service.api.history import history_service_pb2, history_service_pb2_grpc
from agentplane.proto import session_pb2
from agentplane.session_dispatcher import api
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.system_tests.driver.action_cli import CLI
from agentplane.system_tests.driver.cleanup_diagnostics import capture_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_operations import session_until
from agentplane.system_tests.driver.dispatcher_transport import forward, service_caller
from agentplane.system_tests.driver.environment_transport import ENVIRONMENT_PORT, ENVIRONMENT_SERVICE
from agentplane.system_tests.driver.identity import (
    LAUNCH_DEFAULT_POLICY_SETS,
    ScenarioIdentity,
    create_scenario_identity,
    delete_scenario_identity,
    scenario_identity,
)
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_NAME, NATIVE_CLIENT_SVID_SECRET
from agentplane.system_tests.driver.sandbox import ENVIRONMENT_RUNTIME_CONTAINER, POD_TEMPLATE
from agentplane.system_tests.driver.sandbox_diagnostics import container_mount_sources
from agentplane.system_tests.driver.sandbox_object import create_sandbox_object
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.testing.scripted_model import Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _retry_list_sessions_poll_error(error: BaseException) -> bool:
    """Retry an unsettled snapshot or a transient failure from the read-only poll."""
    return isinstance(error, AssertionError) or (
        isinstance(error, grpc.aio.AioRpcError) and error.code() == grpc.StatusCode.UNAVAILABLE
    )


async def _list_hosted_sessions(
    runner: harness_host_pb2_grpc.HarnessHostStub, metadata: tuple[tuple[str, str], ...]
) -> harness_host_pb2.ListSessionsResponse:
    call = runner.ListSessions(harness_host_pb2.ListSessionsRequest(), metadata=metadata, timeout=15)
    headers = await call.initial_metadata()
    if await call.code() == grpc.StatusCode.OK:
        versions = [value for name, value in headers if name == "x-agentplane-harness-host-session-version"]
        if versions != ["1"]:
            raise RuntimeError(f"Unsupported HarnessHost session status version: {versions}")
    return await call


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
    resource = await create_sandbox_object(cluster, name, template=POD_TEMPLATE, identity=identity, running=True)
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
                await pods.list_namespaced_pod(cluster.namespace, label_selector="app.kubernetes.io/name=sessions")
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
    history: history_service_pb2_grpc.HistoryServiceAsyncStub, session: str
) -> tuple[int, set[str], set[str]]:
    cursor = 0
    commands: set[str] = set()
    completed_turns: set[str] = set()
    while True:
        page = await history.ReadSessionEvents(
            history_service_pb2.ReadSessionEventsRequest(session_id=session, after_cursor=cursor, limit=1000),
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


def _metadata(cluster: Cluster, name: str, uid: str) -> tuple[tuple[str, str], ...]:
    return (
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
    app, _, _ = await service_caller(cluster, NATIVE_CLIENT_NAME, certificate_secret=NATIVE_CLIENT_SVID_SECRET)
    gateway, _, _ = await service_caller(cluster, "sessions", certificate_secret="sessions-client-workload-tls")
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
        target = await stack.enter_async_context(forward(cluster))
        channel = await stack.enter_async_context(
            dial(
                target,
                channel_credentials=app.channel_credentials,
                server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
            )
        )
        runtime_target = await stack.enter_async_context(
            forward(cluster, f"service/{ENVIRONMENT_SERVICE}", remote_port=ENVIRONMENT_PORT)
        )
        runtime_channel = await stack.enter_async_context(
            dial(
                runtime_target,
                channel_credentials=gateway.channel_credentials,
                server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
            )
        )
        history_target = await stack.enter_async_context(forward(cluster, "service/history-service"))
        history_channel = await stack.enter_async_context(
            dial(
                history_target,
                channel_credentials=app.channel_credentials,
                server_name=f"history-service.{cluster.namespace}.svc.cluster.local",
            )
        )
        history: history_service_pb2_grpc.HistoryServiceAsyncStub = history_service_pb2_grpc.HistoryServiceStub(
            history_channel
        )
        dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub = (
            session_dispatcher_pb2_grpc.SessionDispatcherStub(channel)
        )
        runner: harness_host_pb2_grpc.HarnessHostStub = harness_host_pb2_grpc.HarnessHostStub(runtime_channel)
        metadata = _metadata(cluster, name, uid)
        await dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec), timeout=15
        )
        await session_until(
            dispatcher,
            session,
            lambda row: (
                row.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED
            ),
        )
        admitted = await runner.SubmitInput(
            harness_host_pb2.SubmitInputRequest(
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
                    snapshot = await _list_hosted_sessions(runner, metadata)
                    active = next((item for item in snapshot.sessions if item.session_id == session), None)
                    assert active is not None
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
            harness_host_pb2.InterruptTurnRequest(session_id=session, command_id=delayed, turn_id=turn_id),
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
                # A new mutation proves this proxy observed deletion while the earlier call stays held.
                async for attempt in AsyncRetrying(
                    stop=stop_after_delay(30), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
                ):
                    with attempt:
                        with pytest.raises(grpc.aio.AioRpcError) as refused:
                            await runner.ChangeReasoningEffort(
                                harness_host_pb2.ChangeReasoningEffortRequest(
                                    session_id=probe_session, command_id=observed, effort="low"
                                ),
                                metadata=metadata,
                                timeout=15,
                            )
                        assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                evidence["proxy_observed_deletion"] = True
                async with cluster.api_client() as client:
                    objects = k8s.CustomObjectsApi(client)
                    async for attempt in AsyncRetrying(
                        stop=stop_after_delay(90), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
                    ):
                        with attempt:
                            sandbox = await objects.get_namespaced_custom_object(
                                GROUP, VERSION, cluster.namespace, "sandboxes", name
                            )
                            assert sandbox["metadata"]["uid"] == uid
                            assert sandbox["metadata"].get("deletionTimestamp") == evidence["deletion_timestamp"]
                            assert sandbox.get("status", {}).get("quiescedIncarnation") == incarnation["uid"]
                sandbox_status = sandbox.get("status", {})
                (artifact / "quiesced-sandbox-status.json").write_text(json.dumps(sandbox_status, indent=2) + "\n")
                pod = json.loads(await cluster.kubectl("get", "pod", incarnation["name"], "-o", "json"))
                assert pod["metadata"]["uid"] == incarnation["uid"]
                assert not pod["metadata"].get("deletionTimestamp")
                before = await _read_sealed_history(history, session)
                assert positive in before[1]
                assert delayed not in before[1]
                assert turn_id in before[2]
                assert not call.done()
                evidence["quiesced_before_release"] = {"sandbox_uid": uid, "status": sandbox_status}
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
                assert await _read_sealed_history(history, session) == before
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
                    session_dispatcher_pb2.GetSessionRequest(session_id=session), timeout=15
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
            session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec), timeout=15
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
            timeout=15,
        )
        replacement_metadata = _metadata(cluster, name, replacement)
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception(_retry_list_sessions_poll_error)
        ):
            with attempt:
                sessions = await _list_hosted_sessions(runner, replacement_metadata)
                assert replacement_session in {item.session_id for item in sessions.sessions}
        replacement_admitted = await runner.ChangeReasoningEffort(
            harness_host_pb2.ChangeReasoningEffortRequest(
                session_id=replacement_session, command_id=str(uuid4()), effort="low"
            ),
            metadata=replacement_metadata,
            timeout=15,
        )
        assert replacement_admitted.admission.event.HasField("command_admitted")
        # This RPC has no Session ID: NOT_FOUND proves UID routing refused the request, even if
        # an installation retains old Sessions across a name replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale_read:
            await _list_hosted_sessions(runner, metadata)
        assert stale_read.value.code() == grpc.StatusCode.NOT_FOUND
        # Preserve the delayed call's payload and old UID; it must not reach this healthy replacement.
        with pytest.raises(grpc.aio.AioRpcError) as stale:
            await runner.InterruptTurn(
                harness_host_pb2.InterruptTurnRequest(session_id=session, command_id=delayed, turn_id=turn_id),
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


async def _wait_retained_turn(history: history_service_pb2_grpc.HistoryServiceAsyncStub, session: str) -> int:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(120), wait=wait_fixed(0.1), retry=retry_if_exception_type(AssertionError)
    ):
        with attempt:
            try:
                page = await history.ReadSessionEvents(
                    history_service_pb2.ReadSessionEventsRequest(session_id=session, after_cursor=0, limit=1000),
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
    history: history_service_pb2_grpc.HistoryServiceAsyncStub,
    dispatcher: session_dispatcher_pb2_grpc.SessionDispatcherAsyncStub,
    runner: harness_host_pb2_grpc.HarnessHostStub,
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
        session_dispatcher_pb2.CreateSessionRequest(session_id=session, spec=spec), timeout=15
    )
    await session_until(
        dispatcher,
        session,
        lambda row: row.status.materialization == session_dispatcher_pb2.SessionStatus.SESSION_MATERIALIZATION_ACCEPTED,
    )
    metadata = _metadata(cluster, sandbox, sandbox_uid)
    admitted = await runner.SubmitInput(
        harness_host_pb2.SubmitInputRequest(session_id=session, command_id=str(uuid4()), text=marker),
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
            snapshot = await _list_hosted_sessions(runner, metadata)
            row = next((item for item in snapshot.sessions if item.session_id == session), None)
            assert row is not None
            assert not row.active_turn_id, row
    await _wait_retained_turn(history, session)
    return session


async def managed_cli_reads_retained_history_with_identity_uid_scope(
    cluster: Cluster, scripted: ScriptedUpstream
) -> None:
    """The mounted CLI reads its own retained Session without the App and cannot cross Identity UIDs."""
    app_resources = await cluster.kubectl("get", "deployment/app", "service/app", "--ignore-not-found", "-o", "name")
    assert not app_resources, f"the native History case requires the App to be absent: {app_resources}"
    artifact = undeclared_outputs_dir() / f"managed-cli-history-{uuid4().hex[:8]}"
    artifact.mkdir()
    evidence: dict[str, object] = {"passed": False}

    def persist() -> None:
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")

    persist()
    async with scripted.case_scope():
        app, _, _ = await service_caller(cluster, NATIVE_CLIENT_NAME, certificate_secret=NATIVE_CLIENT_SVID_SECRET)
        gateway, _, _ = await service_caller(cluster, "sessions", certificate_secret="sessions-client-workload-tls")
        async with (
            forward(cluster) as dispatcher_target,
            forward(cluster, f"service/{ENVIRONMENT_SERVICE}", remote_port=ENVIRONMENT_PORT) as runtime_target,
            forward(cluster, "service/history-service") as history_target,
            AsyncExitStack() as identities,
        ):
            dispatcher_channel = await identities.enter_async_context(
                dial(
                    dispatcher_target,
                    channel_credentials=app.channel_credentials,
                    server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
                )
            )
            runtime_channel = await identities.enter_async_context(
                dial(
                    runtime_target,
                    channel_credentials=gateway.channel_credentials,
                    server_name=f"{ENVIRONMENT_SERVICE}.{cluster.namespace}.svc.cluster.local",
                )
            )
            history_channel = await identities.enter_async_context(
                dial(
                    history_target,
                    channel_credentials=app.channel_credentials,
                    server_name=f"history-service.{cluster.namespace}.svc.cluster.local",
                )
            )
            dispatcher = session_dispatcher_pb2_grpc.SessionDispatcherStub(dispatcher_channel)
            runner: harness_host_pb2_grpc.HarnessHostStub = harness_host_pb2_grpc.HarnessHostStub(runtime_channel)
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
                    history=history,
                    dispatcher=dispatcher,
                    runner=runner,
                    sandbox=shared_name,
                    sandbox_uid=old_uid,
                    pod=old_pod,
                    marker=f"old-identity-{uuid4().hex}",
                )
            old_cursor = await _wait_retained_turn(history, old_session)
            await delete_scenario_identity(cluster, old_identity)
            await _wait_identity_and_binding_deleted(cluster, old_identity)
            old_cursor_after_identity_delete = await _wait_retained_turn(history, old_session)

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
                    history=history,
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
                    history=history,
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

                async def run_cli(*args: str) -> dict[str, Any]:
                    output = await cluster.exec(
                        current_pod, CLI, "--json", *args, container=ENVIRONMENT_RUNTIME_CONTAINER
                    )
                    page: dict[str, Any] = json.loads(output)
                    return page

                summary = await run_cli("history", "get-session-summary", current_session)
                assert summary.get("sessionId") == current_session, summary
                own_page = await run_cli("history", "read-session-events", current_session, "--limit", "1000")
                assert current_marker in json.dumps(own_page), own_page
                assert own_page.get("entries"), own_page

                async def refused_history(session_id: str) -> dict[str, Any]:
                    output = await cluster.exec(
                        current_pod,
                        "sh",
                        "-c",
                        '"$@" 2>&1 || true',
                        "history-client",
                        CLI,
                        "--json",
                        "history",
                        "read-session-events",
                        session_id,
                        container=ENVIRONMENT_RUNTIME_CONTAINER,
                    )
                    result: dict[str, Any] = json.loads(output)
                    return result

                other_page = await refused_history(other_session)
                old_page = await refused_history(old_session)
                for target_session, page in ((other_session, other_page), (old_session, old_page)):
                    assert page.get("error", {}).get("code") == "NOT_FOUND", (target_session, page)
                    assert page.get("error", {}).get("source") == "service", (target_session, page)
                evidence.update(
                    {
                        "app_deployment_and_service_absent": True,
                        "current_identity_history_summary": summary,
                        "current_identity_own_read": own_page,
                        "other_identity_read_refusal": other_page,
                        "old_identity_uid_read_refusal": old_page,
                        "passed": True,
                    }
                )
                persist()
