"""Dispatcher admission through a real k3s Sandbox executor to retained central-sampler History."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
from kubernetes_asyncio import client as k8s, watch as k8s_watch

from agentplane.grpc.calls import ServiceError, dial
from agentplane.history_service import history_service_pb2
from agentplane.native.codex.testing import responses_sse
from agentplane.proto import event_log_pb2, event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.system_tests.central_sampler import MODEL, CentralStack, capture_sampler_diagnostics, central_stack
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.executor import Executor
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import ModelRequest, Reasoning, ShellCall, Text
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _verify_pod_executor(output: str) -> None:
    assert "prepared-on-executor\n" in output
    assert "tool-on-executor\n" in output
    assert "CENTRAL_EXECUTOR_OK\n" in output


def _verify_vm_executor(output: str) -> dict[str, str | int]:
    assert "prepared-on-executor\n" in output
    assert "VM_PID1 systemd\n" in output
    assert "VM_EXECUTOR_UID 107\n" in output
    (cgroup_line,) = [line for line in output.splitlines() if line.startswith("VM_EXECUTOR_CGROUP ")]
    executor_cgroup = cgroup_line.removeprefix("VM_EXECUTOR_CGROUP ")
    assert executor_cgroup.startswith("/agentplane.slice/agentplane-executors.slice/agentplane-native-"), (
        executor_cgroup
    )
    assert executor_cgroup.count("/") == 3, executor_cgroup
    assert executor_cgroup.endswith(".service"), executor_cgroup
    return {"pid1": "systemd", "uid": 107, "executor_cgroup": executor_cgroup}


@dataclass(frozen=True)
class ExecutionProbe:
    command_prefix: str
    verify: Callable[[str], dict[str, str | int] | None]


@dataclass(frozen=True)
class PodModelRequest:
    pod_name: str
    model: CodexModel = field(repr=False)
    request: ModelRequest[Any] = field(repr=False)


class SamplerModelPool:
    """Collect scripted provider requests from whichever sampler replica owns a Session."""

    def __init__(self, scripted: ScriptedUpstream, pod_names: tuple[str, ...]) -> None:
        self._scripted = scripted
        self._requests: asyncio.Queue[PodModelRequest | Exception] = asyncio.Queue()
        self._models: dict[str, CodexModel] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.add_pods(pod_names)

    def add_pods(self, pod_names: tuple[str, ...]) -> None:
        for pod_name in pod_names:
            if pod_name in self._models:
                continue
            model = self._scripted.model(pod_name, runner_pb2.HARNESS_CODEX)
            assert isinstance(model, CodexModel)
            self._models[pod_name] = model
            self._tasks[pod_name] = asyncio.create_task(self._collect(pod_name, model))

    async def _collect(self, pod_name: str, model: CodexModel) -> None:
        while True:
            try:
                request = await model.request()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._requests.put(error)
                return
            await self._requests.put(PodModelRequest(pod_name, model, request))

    async def request(self) -> PodModelRequest:
        async with asyncio.timeout(60):
            request = await self._requests.get()
        if isinstance(request, Exception):
            raise request
        return request

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def __aenter__(self) -> SamplerModelPool:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


POD_EXECUTION = ExecutionProbe(command_prefix="", verify=_verify_pod_executor)
VM_EXECUTION = ExecutionProbe(
    command_prefix=(
        "printf 'VM_PID1 %s\\n' \"$(cat /proc/1/comm)\"; "
        "printf 'VM_EXECUTOR_UID %s\\n' \"$(id -u)\"; "
        "printf 'VM_EXECUTOR_CGROUP %s\\n' \"$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)\"; "
    ),
    verify=_verify_vm_executor,
)


async def _capture_session_diagnostics(
    cluster: Cluster, deployed: CentralStack, session_id: str, command_id: str, admission: str, *, phase: str
) -> None:
    """Retain sampler, History, Sandbox, and full event evidence for a Session."""
    diagnostic: dict[str, object] = {
        "phase": phase,
        "session_id": session_id,
        "command_id": command_id,
        "dispatcher_admission": admission,
    }
    try:
        pod_artifacts = await capture_sampler_diagnostics(cluster)
        diagnostic["sampler_pod_artifacts"] = str(pod_artifacts.relative_to(undeclared_outputs_dir()))
    except Exception as error:
        diagnostic["sampler_pod_artifacts_error"] = repr(error)
    try:
        snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=5
        )
        diagnostic["sampler_session"] = str(snapshot)
    except Exception as error:
        diagnostic["sampler_session_error"] = repr(error)
    try:
        summary = await deployed.history.get_session_summary(session_id)
        diagnostic["history_summary"] = str(summary)
    except Exception as error:
        diagnostic["history_summary_error"] = repr(error)
    try:
        events = await deployed.history.read_session_events(session_id, limit=1000)
        diagnostic["history_events"] = [
            {
                "cursor": entry.cursor,
                "source_id": entry.origin.source_id,
                "sequence": entry.origin.sequence,
                "kind": entry.event.WhichOneof("observation"),
                "event": str(entry.event),
            }
            for entry in events.entries
        ]

    except Exception as error:
        diagnostic["history_events_error"] = repr(error)
    try:
        sandbox_state = json.loads(await cluster.kubectl("get", "sandbox", deployed.sandbox, "-o", "json"))
        diagnostic["sandbox"] = {
            "name": sandbox_state["metadata"]["name"],
            "uid": sandbox_state["metadata"]["uid"],
            "status": sandbox_state.get("status", {}),
        }
    except Exception as error:
        diagnostic["sandbox_error"] = repr(error)
    try:
        runner_lease = await cluster.kubectl("get", "lease", f"{deployed.sandbox}-runner", "-o", "json")
        diagnostic["runner_lease"] = json.loads(runner_lease)
    except Exception as error:
        diagnostic["runner_lease_error"] = repr(error)
    try:
        diagnostic["cluster_services_endpoints_pods"] = json.loads(
            await cluster.kubectl("get", "services,endpointslices,pods", "-o", "json")
        )
    except Exception as error:
        diagnostic["cluster_services_endpoints_pods_error"] = repr(error)
    for component in ("runner-gateway", "history-service"):
        try:
            diagnostic[f"{component}_logs"] = await cluster.kubectl(
                "logs", f"deployment/{component}", "--all-containers", "--tail=100", "--since=10m"
            )
        except Exception as error:
            diagnostic[f"{component}_logs_error"] = repr(error)
    (undeclared_outputs_dir() / f"central-sampler-session-diagnostics-{session_id}.json").write_text(
        json.dumps(diagnostic, indent=2) + "\n"
    )


async def _wait_for_setup(deployed: CentralStack, session_id: str) -> None:
    """Wait for setup's retained terminal event before starting the short-timeout provider read."""
    async with asyncio.timeout(60), aclosing(deployed.history.watch_session_summaries()) as changes:
        async for _ in changes:
            try:
                retained = await deployed.history.read_session_events(session_id, limit=1000)
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                # Dispatcher admission can precede this Session's first History export.
                continue
            for entry in retained.entries:
                if entry.event.HasField("setup_interrupted"):
                    raise AssertionError(f"sampler setup was interrupted for Session {session_id}")
                if entry.event.HasField("harness_launch_failed"):
                    raise AssertionError(f"sampler could not start setup for Session {session_id}")
                if entry.event.HasField("setup_finished"):
                    exit_code = entry.event.setup_finished.exit_code
                    assert exit_code == 0, f"sampler setup for Session {session_id} exited {exit_code}"
                    return
        raise TimeoutError("History watch ended before sampler setup finished")


async def _wait_for_execution_progress(deployed: CentralStack, session_id: str, kind: str, phase: str) -> None:
    events = deployed.sampler.FollowEvents(
        sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True),
        metadata=deployed.sampler_metadata,
        timeout=60,
    )
    try:
        async for frame in events:
            event = frame.entry.event
            if event.HasField("execution_progress"):
                progress = event.execution_progress
                if progress.kind == kind and progress.phase == phase:
                    return
        raise TimeoutError(f"History ended before Session {session_id} {kind} reached {phase!r}")
    finally:
        events.cancel()


async def _sampler_code_mode_host_pid(cluster: Cluster, pod_name: str) -> tuple[Path, str]:
    """Read the shared host PID through the sampler Pod's existing diagnostic EmptyDir."""
    pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
    pod_uid = pod["metadata"]["uid"]
    pid_file = Path(f"/var/lib/kubelet/pods/{pod_uid}/volumes/kubernetes.io~empty-dir/logs/code-mode-host.pid")
    pid = await asyncio.to_thread(pid_file.read_text)
    pid = pid.strip()
    assert pid.isdecimal(), f"invalid code-mode host PID {pid!r}"
    return pid_file, pid


async def _read_code_mode_host_pid(pid_file: Path) -> str:
    return (await asyncio.to_thread(pid_file.read_text)).strip()


async def _wait_for_turn_status(
    deployed: CentralStack, session_id: str, turn_id: str, status: event_pb2.TurnStatus
) -> None:
    events = deployed.sampler.FollowEvents(
        sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True),
        metadata=deployed.sampler_metadata,
        timeout=60,
    )
    try:
        async for frame in events:
            event = frame.entry.event
            if event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                assert event.turn_completed.status == status, event.turn_completed
                return
        raise TimeoutError(f"History ended before turn {turn_id} completed")
    finally:
        events.cancel()


async def _wait_for_history_turn_completed(
    deployed: CentralStack, session_id: str, turn_id: str
) -> event_log_pb2.EventEntry:
    async with asyncio.timeout(60), aclosing(deployed.history.follow_session(session_id)) as events:
        async for frame in events:
            for entry in frame.entries.entries:
                event = entry.event
                if event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    assert event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED, event.turn_completed
                    return cast(event_log_pb2.EventEntry, entry)
        raise TimeoutError(f"History ended before turn {turn_id} completed")


async def _follow_sampler_logs(cluster: Cluster, pod_name: str) -> AsyncGenerator[dict[str, Any]]:
    process = await asyncio.create_subprocess_exec(
        *cluster.command("logs", pod_name, "--container=central-sampler", "--follow"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    try:
        async for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event
        stderr = b"" if process.stderr is None else await process.stderr.read()
        await process.wait()
        if process.returncode:
            raise RuntimeError(f"kubectl logs for {pod_name} exited {process.returncode}: {stderr.decode()}")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


def _pod_ready(pod: k8s.V1Pod) -> bool:
    return (
        pod.metadata.deletion_timestamp is None
        and pod.status is not None
        and any(condition.type == "Ready" and condition.status == "True" for condition in pod.status.conditions or [])
    )


async def _wait_for_sampler_rollout_overlap(cluster: Cluster, *, old_pod_uid: str, old_revision: str) -> str:
    """Wait until the new revision is Ready while the current Session owner remains Ready."""
    async with cluster.api_client() as connection:
        core = k8s.CoreV1Api(connection)
        pods = await core.list_namespaced_pod(
            cluster.namespace, label_selector="app.kubernetes.io/name=central-sampler"
        )
        current = {str(pod.metadata.uid): pod for pod in pods.items}

        def overlap() -> k8s.V1Pod | None:
            old = current.get(old_pod_uid)
            replacement = next(
                (
                    pod
                    for pod in current.values()
                    if pod.metadata.labels.get("pod-template-hash") != old_revision and _pod_ready(pod)
                ),
                None,
            )
            if replacement is None:
                return None
            assert old is not None
            assert _pod_ready(old), "old Session owner left before replacement became Ready"
            return replacement

        replacement = overlap()
        if replacement is not None:
            return str(replacement.metadata.name)
        if not pods.metadata.resource_version:
            raise RuntimeError("Kubernetes omitted the sampler Pod list resource version")
        async with asyncio.timeout(120), k8s_watch.Watch() as watch:
            async for event in watch.stream(
                core.list_namespaced_pod,
                cluster.namespace,
                label_selector="app.kubernetes.io/name=central-sampler",
                resource_version=pods.metadata.resource_version,
                timeout_seconds=120,
            ):
                pod = event["object"]
                uid = str(pod.metadata.uid)
                if event["type"] == "DELETED":
                    current.pop(uid, None)
                else:
                    current[uid] = pod
                replacement = overlap()
                if replacement is not None:
                    return str(replacement.metadata.name)
        raise TimeoutError("central sampler rollout never had a Ready old/new Pod overlap")


async def _sampler_claim_event(cluster: Cluster, pod_name: str, session_id: str) -> dict[str, Any]:
    output = await cluster.kubectl("logs", pod_name, "--container=central-sampler")
    matching = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("msg") == "central sampler lease claimed"
            and event.get("session_id") == session_id
        ):
            matching.append(event)
    if not matching:
        raise AssertionError(f"{pod_name} logs have no lease-claim event for Session {session_id}")
    return matching[-1]


async def _get_session_from_pod(
    cluster: Cluster, deployed: CentralStack, pod_name: str, session_id: str
) -> sampler_pb2.GetSessionResponse:
    pod = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
    target = f"{pod['status']['podIP']}:8080"
    async with dial(target) as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout=10)
        sampler = sampler_pb2_grpc.SamplerStub(channel)
        return cast(
            sampler_pb2.GetSessionResponse,
            await sampler.GetSession(
                sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
            ),
        )


async def _capture_rollout_followup_diagnostics(
    cluster: Cluster, deployed: CentralStack, pod_names: tuple[str, ...], session_id: str, command_id: str
) -> Path:
    evidence: dict[str, object] = {"session_id": session_id, "command_id": command_id, "pods": {}}
    pods = cast(dict[str, object], evidence["pods"])
    for pod_name in pod_names:
        pod_evidence: dict[str, object] = {}
        pods[pod_name] = pod_evidence
        try:
            pod_state = json.loads(await cluster.kubectl("get", "pod", pod_name, "-o", "json"))
            containers = [container["name"] for container in pod_state["spec"].get("containers", [])]
            containers.extend(container["name"] for container in pod_state["spec"].get("initContainers", []))
            pod_evidence["container_logs"] = {
                container: await cluster.kubectl("logs", pod_name, "--container", container) for container in containers
            }
        except Exception as error:
            pod_evidence["container_logs_error"] = repr(error)
        try:
            snapshot = await _get_session_from_pod(cluster, deployed, pod_name, session_id)
            pod_evidence["session"] = str(snapshot)
            async with dial(
                f"{json.loads(await cluster.kubectl('get', 'pod', pod_name, '-o', 'json'))['status']['podIP']}:8080"
            ) as channel:
                sampler = sampler_pb2_grpc.SamplerStub(channel)
                command = await sampler.GetCommand(
                    sampler_pb2.GetCommandRequest(session_id=session_id, command_id=command_id),
                    metadata=deployed.sampler_metadata,
                    timeout=10,
                )
                pod_evidence["command"] = str(command)
        except Exception as error:
            pod_evidence["sampler_error"] = repr(error)
    try:
        dispatch = await deployed.dispatcher.GetDispatch(
            session_dispatcher_pb2.GetDispatchRequest(command_id=command_id), metadata=deployed.metadata, timeout=10
        )
        evidence["dispatch"] = str(dispatch)
    except Exception as error:
        evidence["dispatch_error"] = repr(error)
    try:
        summary = await deployed.history.get_session_summary(session_id)
        evidence["history_summary"] = str(summary)
    except Exception as error:
        evidence["history_summary_error"] = repr(error)
    try:
        events = await deployed.history.read_session_events(session_id, limit=1000)
        evidence["history_events"] = [
            {
                "cursor": entry.cursor,
                "source_id": entry.origin.source_id,
                "sequence": entry.origin.sequence,
                "kind": entry.event.WhichOneof("observation"),
                "event": str(entry.event),
            }
            for entry in events.entries
        ]
    except Exception as error:
        evidence["history_events_error"] = repr(error)
    try:
        evidence["cluster_services_endpoints_pods"] = json.loads(
            await cluster.kubectl("get", "services,endpointslices,pods", "-o", "json")
        )
    except Exception as error:
        evidence["cluster_services_endpoints_pods_error"] = repr(error)
    try:
        resources = json.loads(await cluster.kubectl("get", "pods", "-o", "json"))
        postgres_pods = [
            pod["metadata"]["name"]
            for pod in resources["items"]
            if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "postgres"
        ]
        evidence["postgres_logs"] = {
            pod_name: await cluster.kubectl("logs", pod_name, "--all-containers") for pod_name in postgres_pods
        }
    except Exception as error:
        evidence["postgres_logs_error"] = repr(error)
    output = undeclared_outputs_dir() / "central-sampler-rollout-followup-diagnostics.json"
    output.write_text(json.dumps(evidence, indent=2) + "\n")
    return output


async def _wait_for_folded_history(deployed: CentralStack, session_id: str, cursor: int) -> Any:
    async with asyncio.timeout(60), aclosing(deployed.history.watch_session_summaries()) as changes:
        async for _ in changes:
            try:
                summary = await deployed.history.get_session_summary(session_id)
            except ServiceError as error:
                if error.code != grpc.StatusCode.NOT_FOUND:
                    raise
                continue
            if summary.folded_through >= cursor:
                return summary
        raise TimeoutError(f"History watch ended before Session {session_id} folded through cursor {cursor}")


async def dispatcher_central_setup_tool_and_retained_answer(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream, executor: Executor, execution_probe: ExecutionProbe
) -> None:
    template, cwd = executor.template, executor.cwd
    session_id, command_id = str(uuid4()), str(uuid4())
    interrupted_session_id, interrupted_command_id = str(uuid4()), str(uuid4())
    prompt = "Read the setup marker and report the result."
    reasoning = "I will read the marker created during setup."
    answer = "The executor returned prepared-on-executor."
    async with (
        central_stack(cluster, tmp_path, template=template) as deployed,
        SamplerModelPool(scripted, deployed.sampler_pod_names) as models,
    ):
        session_spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CENTRAL_SAMPLER, cwd=cwd, model=MODEL)
        interrupted_opening = await deployed.dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(
                session_id=interrupted_session_id, sandbox=deployed.sandbox, spec=session_spec, setup_script=":\n"
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert interrupted_opening.opening.sandbox_uid == deployed.sandbox_uid
        interrupted_input = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=interrupted_session_id,
                sandbox=deployed.sandbox,
                sandbox_uid=deployed.sandbox_uid,
                command_id=interrupted_command_id,
                text="Start a long-running executor command and keep this turn active.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert interrupted_input.dispatch.command_id == interrupted_command_id
        await _wait_for_setup(deployed, interrupted_session_id)
        interrupted_exchange = await models.request()
        interrupted_request = interrupted_exchange.request
        assert interrupted_request.native.client_metadata.session_id == interrupted_session_id
        assert interrupted_request.native.code_mode
        async with interrupted_request._exchange as exchange:
            await exchange.send(
                *responses_sse.response_stream(
                    [
                        responses_sse.Reasoning(
                            "I will keep this executor command running until the turn is interrupted.",
                            "test-opaque-interrupted-session-reasoning",
                        ),
                        responses_sse.CustomToolCall(
                            "long-lived-executor-cell",
                            "exec",
                            # exec_command yields after at most 30s; keep polling so the cell
                            # stays live for the whole command.
                            '// @exec: {"yield_time_ms": 1}\n'
                            'let result = await tools.exec_command({"cmd":"sleep 120"});\n'
                            "while (result.session_id !== undefined) {\n"
                            "  result = await tools.write_stdin("
                            '{"session_id": result.session_id, "yield_time_ms": 300000});\n'
                            "}",
                        ),
                    ],
                    model=MODEL,
                ).events
            )
        await _wait_for_execution_progress(deployed, interrupted_session_id, "cell", "live")
        host_pid_file, host_pid = await _sampler_code_mode_host_pid(cluster, interrupted_exchange.pod_name)
        # A yielded cell returns control to sampling. Hold that continuation explicitly so
        # B's request is the next scripted exchange and A remains active until interruption.
        continuation_exchange = await models.request()
        continuation = continuation_exchange.request
        assert continuation.native.client_metadata.session_id == interrupted_session_id
        await continuation_exchange.model.hold(continuation)

        opening = await deployed.dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                spec=session_spec,
                setup_script="printf 'prepared-on-executor\\n' > prepared.txt\nprintf 'setup-on-executor\\n'\n",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert opening.opening.sandbox_uid == deployed.sandbox_uid
        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                sandbox_uid=deployed.sandbox_uid,
                command_id=command_id,
                text=prompt,
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == command_id
        changes = deployed.dispatcher.WatchDispatches(
            session_dispatcher_pb2.WatchDispatchesRequest(
                filter=session_dispatcher_pb2.DispatchFilter(session_id=session_id)
            ),
            metadata=deployed.metadata,
            timeout=60,
        )
        admission = ""
        try:
            async for change in changes:
                if not change.HasField("dispatch"):
                    continue
                delivery = change.dispatch
                assert delivery.state not in (
                    session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                    session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
                ), delivery
                if delivery.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED:
                    assert delivery.admission.event.command_admitted.command.submit_input.text == prompt
                    assert delivery.sandbox_uid == deployed.sandbox_uid
                    admission = str(delivery)
                    break
            else:
                pytest.fail("Dispatcher watch ended before central admission")
        finally:
            changes.cancel()
        try:
            await _wait_for_setup(deployed, session_id)
            request_exchange = await models.request()
            request = request_exchange.request
        except Exception:
            await _capture_session_diagnostics(
                cluster, deployed, session_id, command_id, admission, phase="setup_or_initial_model_request"
            )
            raise
        assert request.model == MODEL
        assert request.native.client_metadata.session_id == session_id
        assert prompt in request.user_texts
        tool_command = (
            "set -eu; sleep 10; "
            + execution_probe.command_prefix
            + "cat prepared.txt; printf 'tool-on-executor\\n' | tee result.txt; "
            "printf 'CENTRAL_EXECUTOR_OK\\n'"
        )
        try:
            await request_exchange.model.reply(
                request, Reasoning(reasoning), ShellCall("read-prepared-marker", tool_command)
            )
            # Require the executor process to be running before interrupting Session A; B's shell
            # command sleeps for ten seconds so it can finish and return its real-executor output.
            await _wait_for_execution_progress(deployed, session_id, "process", "running")
            assert await _read_code_mode_host_pid(host_pid_file) == host_pid
            interruption = await deployed.dispatcher.InterruptTurn(
                session_dispatcher_pb2.InterruptTurnRequest(
                    command_id=str(uuid4()),
                    session_id=interrupted_session_id,
                    sandbox=deployed.sandbox,
                    sandbox_uid=deployed.sandbox_uid,
                    turn_id=interrupted_command_id,
                ),
                metadata=deployed.metadata,
                timeout=15,
            )
            assert interruption.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
            await _wait_for_turn_status(
                deployed, interrupted_session_id, interrupted_command_id, event_pb2.TURN_STATUS_INTERRUPTED
            )
            assert await _read_code_mode_host_pid(host_pid_file) == host_pid
            tool_result_exchange = await models.request()
            request = tool_result_exchange.request
            (tool_output,) = request.tool_outputs
            output = "\n".join(tool_output.blocks)
            assert "CENTRAL_EXECUTOR_OK" in output
            vm_execution = execution_probe.verify(output)
            assert any(item.encrypted_content for item in request.native.reasoning)
            await tool_result_exchange.model.reply(request, Text(answer))
        except Exception as error:
            try:
                await _capture_session_diagnostics(
                    cluster, deployed, session_id, command_id, admission, phase="tool_result_model_request"
                )
            except Exception as diagnostic_error:
                error.add_note(f"Could not capture central sampler diagnostics: {diagnostic_error!r}")
            raise
        events = deployed.sampler.FollowEvents(
            sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True),
            metadata=deployed.sampler_metadata,
            timeout=30,
        )
        try:
            async for frame in events:
                if frame.entry.event.HasField("turn_completed"):
                    assert frame.entry.event.turn_completed.turn_id == command_id
                    assert frame.entry.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
                    terminal = frame.entry
                    break
            else:
                pytest.fail("Sampler stream ended before completion")
        finally:
            events.cancel()
        snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        assert snapshot.executor.sandbox_uid == deployed.sandbox_uid
        assert snapshot.expected_incarnation_uid == deployed.incarnation_uid
        assert snapshot.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
        async with asyncio.timeout(60), aclosing(deployed.history.watch_session_summaries()) as changes:
            async for _ in changes:
                try:
                    summary = await deployed.history.get_session_summary(session_id)
                except ServiceError as error:
                    if error.code != grpc.StatusCode.NOT_FOUND:
                        raise
                    continue
                if summary.folded_through >= terminal.cursor:
                    break
            else:
                pytest.fail("History watch ended before the final answer was folded")
        assert summary.sandbox.uid == deployed.sandbox_uid
        assert summary.attached.session_id == session_id
        assert summary.last_turn_status == event_pb2.TURN_STATUS_COMPLETED
        retained = await deployed.history.read_session_events(session_id, limit=1000)
        assert any(entry.origin == terminal.origin and entry.event == terminal.event for entry in retained.entries)
        finished = [entry for entry in retained.entries if entry.event.HasField("setup_finished")]
        assert len(finished) == 1
        assert finished[0].event.setup_finished.exit_code == 0
        setup_stdout = b"".join(entry.event.setup_output.stdout for entry in retained.entries)
        if setup_stdout != b"setup-on-executor\n":
            await _capture_session_diagnostics(
                cluster, deployed, session_id, command_id, admission, phase="setup_stdout_assertion"
            )
        assert setup_stdout == b"setup-on-executor\n"
        completed = [entry for entry in retained.entries if entry.event.HasField("item_completed")]
        for text in (reasoning, answer):
            (item,) = [entry for entry in completed if entry.event.item_completed.text == text]
            assert item.event.source_sequences
            assert item.origin.source_id == terminal.origin.source_id
            for sequence in item.event.source_sequences:
                source = next(entry for entry in retained.entries if entry.origin.sequence == sequence)
                assert source.event.HasField("boundary_record")
        assert any(
            entry.event.item_completed.tool.succeeded
            and "prepared-on-executor" in entry.event.item_completed.tool.output
            for entry in completed
        )
        entities = await deployed.history.read_thread_entities(
            history_service_pb2.ReadThreadEntitiesRequest(
                session_id=session_id, range=history_service_pb2.EntityRange(limit=200)
            )
        )
        views = [json.loads(entity.view_json) for entity in entities.entities]
        assert all(view["thread_id"] == session_id for view in views)
        for text in (reasoning, answer):
            (item,) = [entry for entry in completed if entry.event.item_completed.text == text]
            (view,) = [
                view
                for view in views
                if view["entity_kind"] == "item" and view["entity_id"] == item.event.item_completed.item_id
            ]
            assert view["state"]["completion"] == "text"
            assert view["text_ref"] is not None
            assert not view["pending"]
        for kind in (event_pb2.ITEM_KIND_REASONING, event_pb2.ITEM_KIND_ASSISTANT_TEXT, event_pb2.ITEM_KIND_TOOL_CALL):
            assert any(view["entity_kind"] == "item" and view["state"]["kind"] == kind for view in views)
        assert any(
            view["entity_kind"] == "confirmed_input" and command_id in view["state"]["origin_command_ids"]
            for view in views
        )
        (undeclared_outputs_dir() / "central-sampler-k3s.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "command_id": command_id,
                    "sandbox": deployed.sandbox,
                    "sandbox_uid": deployed.sandbox_uid,
                    "incarnation_uid": deployed.incarnation_uid,
                    "template": template,
                    "tool_output": output,
                    "vm_execution": vm_execution,
                    "control_target": deployed.control_target,
                    "source_id": terminal.origin.source_id,
                    "terminal_cursor": terminal.cursor,
                    "folded_through": summary.folded_through,
                    "setup_state": runner_pb2.SetupState.Name(snapshot.attached.setup_state),
                    "passed": True,
                },
                indent=2,
            )
            + "\n"
        )


async def dispatcher_same_session_survives_sampler_rolling_update(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream, executor: Executor
) -> None:
    """Drain an active provider response through a real Deployment rollout, then reuse its Session."""
    session_id, first_command_id, followup_command_id = (str(uuid4()) for _ in range(3))
    prompt = "Add one entry to the activity log, then tell me its line count."
    followup_prompt = "Check the activity log again and tell me its line count."
    first_answer = "I added one activity entry; the log has one line."
    continuation_answer = "The activity log has one line."
    followup_answer = "The activity log still has one line."
    template, cwd = executor.template, executor.cwd
    async with (
        central_stack(cluster, tmp_path, template=template) as deployed,
        SamplerModelPool(scripted, deployed.sampler_pod_names) as models,
    ):
        deployment = json.loads(await cluster.kubectl("get", "deployment", "central-sampler", "-o", "json"))
        assert deployment["spec"]["replicas"] == 2
        assert deployment["spec"]["strategy"]["type"] == "RollingUpdate"
        rolling = deployment["spec"]["strategy"]["rollingUpdate"]
        assert rolling["maxSurge"] in (1, "1")
        assert rolling["maxUnavailable"] in (0, "0")
        assert deployment.get("status", {}).get("readyReplicas") == 2
        assert len(deployed.sampler_pod_names) == 2
        disruption_budget = json.loads(await cluster.kubectl("get", "pdb", "central-sampler", "-o", "json"))
        assert disruption_budget["spec"]["minAvailable"] in (1, "1")

        opening = await deployed.dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                spec=runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CENTRAL_SAMPLER, cwd=cwd, model=MODEL),
                setup_script=":\n",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert opening.opening.sandbox_uid == deployed.sandbox_uid
        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                sandbox_uid=deployed.sandbox_uid,
                command_id=first_command_id,
                text=prompt,
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == first_command_id
        await _wait_for_setup(deployed, session_id)
        initial_snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        assert initial_snapshot.executor.sandbox_uid == deployed.sandbox_uid
        assert initial_snapshot.expected_incarnation_uid == deployed.incarnation_uid
        assert initial_snapshot.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED

        first = await models.request()
        assert first.request.native.client_metadata.session_id == session_id
        assert prompt in first.request.user_texts
        assert first.request.native.code_mode
        await first.model.reply(
            first.request,
            Reasoning("I will add one line and check the count."),
            ShellCall("append-activity-entry", "printf 'task\\n' >> activity.log; wc -l < activity.log"),
        )

        # Reaching the provider with this tool result proves the executor write returned. Keep
        # that ordinary next sampling request unanswered while Kubernetes creates the new revision.
        held = await models.request()
        assert held.request.native.client_metadata.session_id == session_id
        assert len(held.request.tool_outputs) == 1
        assert any(block.strip() == "1" for block in held.request.tool_outputs[0].blocks)

        owner_pod = json.loads(await cluster.kubectl("get", "pod", held.pod_name, "-o", "json"))
        owner_uid = owner_pod["metadata"]["uid"]
        old_revision = owner_pod["metadata"]["labels"]["pod-template-hash"]
        async with aclosing(_follow_sampler_logs(cluster, held.pod_name)) as log_events:
            async with asyncio.timeout(60):
                async for event in log_events:
                    if event.get("msg") == "central sampler lease claimed" and event.get("session_id") == session_id:
                        old_claim = event
                        break
                else:
                    raise TimeoutError("sampler owner log ended before its lease claim")
            assert old_claim.get("pod_name") == held.pod_name
            assert isinstance(old_claim.get("generation"), int)
            assert old_claim.get("owner_token")

            overlap = asyncio.create_task(
                _wait_for_sampler_rollout_overlap(cluster, old_pod_uid=owner_uid, old_revision=old_revision)
            )
            try:
                await cluster.kubectl("rollout", "restart", "deployment/central-sampler")
                replacement_pod = await overlap
            finally:
                if not overlap.done():
                    overlap.cancel()
                    await asyncio.gather(overlap, return_exceptions=True)
            async with asyncio.timeout(60):
                async for event in log_events:
                    if event.get("msg") == "central sampler drain started":
                        assert event.get("pod_name") == held.pod_name
                        assert int(event.get("active_drivers", 0)) >= 1
                        break
                else:
                    raise TimeoutError("sampler owner log ended before the drain-start event")
            assert replacement_pod != held.pod_name
            # Start collecting the new Pod's model endpoint before releasing the held response. The
            # successor may need another request on this same turn after it applies the durable reply.
            models.add_pods((replacement_pod,))

        await held.model.reply(
            held.request, Reasoning("The write completed before the sampler rolled."), Text(first_answer)
        )
        continuation_pod: str | None = None
        try:
            continuation = await models.request()
            continuation_pod = continuation.pod_name
            assert continuation.pod_name != held.pod_name
            assert continuation.request.native.client_metadata.session_id == session_id
            assert continuation.request.native.client_metadata.turn_id == first_command_id
            assert prompt in continuation.request.user_texts
            assert first_answer in continuation.request.assistant_texts
            assert len(continuation.request.tool_outputs) == 1
            assert any(block.strip() == "1" for block in continuation.request.tool_outputs[0].blocks)
            await continuation.model.reply(continuation.request, Text(continuation_answer))
            first_terminal = await _wait_for_history_turn_completed(deployed, session_id, first_command_id)
        except Exception as error:
            async with cluster.api_client() as connection:
                live_pods = await k8s.CoreV1Api(connection).list_namespaced_pod(
                    cluster.namespace, label_selector="app.kubernetes.io/name=central-sampler"
                )
            diagnostic_pods = tuple(
                pod.metadata.name for pod in live_pods.items if pod.metadata.deletion_timestamp is None
            )
            for pod_name in (held.pod_name, replacement_pod, continuation_pod):
                if pod_name is not None and pod_name not in diagnostic_pods:
                    diagnostic_pods += (pod_name,)
            try:
                diagnostic = await _capture_rollout_followup_diagnostics(
                    cluster, deployed, diagnostic_pods, session_id, first_command_id
                )
                error.add_note(f"central sampler rollout diagnostics: {diagnostic}")
            except Exception as diagnostic_error:
                error.add_note(f"Could not capture central sampler rollout diagnostics: {diagnostic_error!r}")
            raise
        await cluster.kubectl("rollout", "status", "deployment/central-sampler", "--timeout=120s")
        current_deployment = json.loads(await cluster.kubectl("get", "deployment", "central-sampler", "-o", "json"))
        assert current_deployment["metadata"]["generation"] == current_deployment["status"]["observedGeneration"]
        assert current_deployment["status"].get("readyReplicas") == 2
        async with cluster.api_client() as connection:
            sampler_pods = await k8s.CoreV1Api(connection).list_namespaced_pod(
                cluster.namespace, label_selector="app.kubernetes.io/name=central-sampler"
            )
        current_pod_names = tuple(pod.metadata.name for pod in sampler_pods.items if _pod_ready(pod))
        assert len(current_pod_names) == 2
        assert held.pod_name not in current_pod_names
        models.add_pods(current_pod_names)
        for pod_name in current_pod_names:
            await _get_session_from_pod(cluster, deployed, pod_name, session_id)

        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                sandbox_uid=deployed.sandbox_uid,
                command_id=followup_command_id,
                text=followup_prompt,
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == followup_command_id
        try:
            followup = await models.request()
        except TimeoutError:
            diagnostic = await _capture_rollout_followup_diagnostics(
                cluster, deployed, current_pod_names, session_id, followup_command_id
            )
            raise AssertionError(
                "Dispatcher accepted the follow-up but no successor sampler reached the scripted provider; "
                f"inspect {diagnostic}"
            ) from None
        assert followup.pod_name in current_pod_names
        assert followup.pod_name != held.pod_name
        assert followup.request.native.client_metadata.session_id == session_id
        assert followup_prompt in followup.request.user_texts
        assert first_answer in followup.request.assistant_texts

        successor_claim = await _sampler_claim_event(cluster, followup.pod_name, session_id)
        assert successor_claim.get("pod_name") == followup.pod_name
        assert successor_claim.get("owner_token") != old_claim.get("owner_token")
        assert int(successor_claim["generation"]) > int(old_claim["generation"])
        successor_snapshot = await _get_session_from_pod(cluster, deployed, followup.pod_name, session_id)
        assert successor_snapshot.executor.sandbox_uid == deployed.sandbox_uid
        assert successor_snapshot.expected_incarnation_uid == deployed.incarnation_uid
        assert successor_snapshot.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED

        await followup.model.reply(
            followup.request,
            Reasoning("I will check the existing log without adding another entry."),
            ShellCall("read-activity-count", "wc -l < activity.log"),
        )
        result = await models.request()
        assert result.request.native.client_metadata.session_id == session_id
        assert len(result.request.tool_outputs) == 1
        assert any(block.strip() == "1" for block in result.request.tool_outputs[0].blocks)
        await result.model.reply(result.request, Text(followup_answer))
        final_terminal = await _wait_for_history_turn_completed(deployed, session_id, followup_command_id)
        summary = await _wait_for_folded_history(deployed, session_id, final_terminal.cursor)
        assert summary.sandbox.uid == deployed.sandbox_uid
        assert summary.attached.session_id == session_id
        assert summary.last_turn_status == event_pb2.TURN_STATUS_COMPLETED
        retained = await deployed.history.read_session_events(session_id, limit=1000)
        assert any(
            entry.cursor == first_terminal.cursor and entry.event == first_terminal.event for entry in retained.entries
        )
        assert any(
            entry.cursor == final_terminal.cursor and entry.event == final_terminal.event for entry in retained.entries
        )
        completed = [entry.event.item_completed for entry in retained.entries if entry.event.HasField("item_completed")]
        assert any(item.text == first_answer for item in completed)
        assert any(item.text == continuation_answer for item in completed)
        assert any(item.text == followup_answer for item in completed)
        tool_results = [item.tool for item in completed if item.WhichOneof("outcome") == "tool"]
        assert len(tool_results) == 2
        assert all(result.succeeded and "1" in result.output for result in tool_results)
        (undeclared_outputs_dir() / "central-sampler-rollout.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "sandbox_uid": deployed.sandbox_uid,
                    "incarnation_uid": deployed.incarnation_uid,
                    "old_owner_pod": held.pod_name,
                    "old_generation": old_claim["generation"],
                    "successor_pod": followup.pod_name,
                    "successor_generation": successor_claim["generation"],
                    "rollout_overlap_pod": replacement_pod,
                    "first_terminal_cursor": first_terminal.cursor,
                    "final_terminal_cursor": final_terminal.cursor,
                    "history_folded_through": summary.folded_through,
                },
                indent=2,
            )
            + "\n"
        )


# gazelle:include_dep @pypi//protobuf
