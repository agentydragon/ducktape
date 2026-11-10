"""Dispatcher admission through a real k3s Sandbox executor to retained central-sampler History."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

import grpc
import pytest

from agentplane.app.testing.client import Client
from agentplane.central_sampler import sampler_pb2
from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import ServiceError
from agentplane.history_service import history_service_pb2
from agentplane.native.codex.testing import responses_sse
from agentplane.proto import event_pb2, identity_pb2, session_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.system_tests.driver.central_sampler import (
    MODEL,
    CentralStack,
    capture_sampler_diagnostics,
    central_stack,
)
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.sandbox import (
    ENVIRONMENT_RUNTIME_LEASE_SUFFIX,
    SANDBOX_READY_SECONDS,
    wait_until_running,
)
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.scripted_model import Reasoning, ShellCall, Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def _verify_pod_executor(output: str) -> None:
    assert "prepared-on-executor\n" in output
    assert "tool-on-executor\n" in output
    assert "CENTRAL_EXECUTOR_OK\n" in output


async def _capture_session_diagnostics(
    cluster: Cluster, deployed: CentralStack, session_id: str, command_id: str, admission: str, *, phase: str
) -> None:
    """Retain sampler, History, Sandbox, and full event evidence for a Session."""
    with tracer.start_as_current_span("system_test.sampler.diagnostics"):
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
            environment_runtime_lease = await cluster.kubectl(
                "get", "lease", f"{deployed.sandbox}{ENVIRONMENT_RUNTIME_LEASE_SUFFIX}", "-o", "json"
            )
            diagnostic["environment_runtime_lease"] = json.loads(environment_runtime_lease)
        except Exception as error:
            diagnostic["environment_runtime_lease_error"] = repr(error)
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
    with tracer.start_as_current_span("system_test.sampler.setup", attributes={"session.id": session_id}):
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


async def _wait_for_execution_progress(
    deployed: CentralStack,
    session_id: str,
    kind: event_pb2.ExecutionProgress.Kind,
    phase: event_pb2.ExecutionProgress.Phase,
) -> None:
    with tracer.start_as_current_span(
        "system_test.sampler.execution",
        attributes={
            "session.id": session_id,
            "execution.kind": event_pb2.ExecutionProgress.Kind.Name(kind),
            "execution.phase": event_pb2.ExecutionProgress.Phase.Name(phase),
        },
    ):
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
            raise TimeoutError(f"History ended before Session {session_id=} {kind=} reached {phase=}")
        finally:
            events.cancel()


async def _sampler_code_mode_host_pid(cluster: Cluster, deployed: CentralStack) -> tuple[Path, str]:
    """Read the shared host PID through the sampler Pod's existing diagnostic EmptyDir."""
    pod = json.loads(await cluster.kubectl("get", "pod", deployed.sampler_pod_name, "-o", "json"))
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
    with tracer.start_as_current_span("system_test.sampler.turn"):
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


async def dispatcher_central_setup_tool_and_retained_answer(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream, executor: Executor
) -> None:
    template, cwd = executor.template, executor.cwd
    session_id, command_id = str(uuid4()), str(uuid4())
    interrupted_session_id, interrupted_command_id = str(uuid4()), str(uuid4())
    prompt = "Read the setup marker and report the result."
    reasoning = "I will read the marker created during setup."
    answer = "The executor returned prepared-on-executor."
    async with central_stack(cluster, tmp_path, template=template) as deployed:
        model = scripted.model(deployed.sampler_pod_name, session_pb2.HARNESS_CODEX)
        assert isinstance(model, CodexModel)
        session_spec = session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                central_sampler_v1=session_pb2.CentralSamplerV1Placement(
                    executor_environment=session_pb2.ExactEnvironment(name=deployed.sandbox, uid=deployed.sandbox_uid)
                )
            ),
            cwd=cwd,
            model=MODEL,
        )
        interrupted_created = await deployed.dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(
                session_id=interrupted_session_id, spec=session_spec, setup_script=":\n"
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert interrupted_created.session.placement.central_sampler_v1.executor_environment.uid == deployed.sandbox_uid
        interrupted_input = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=interrupted_session_id,
                expected_sandbox_uid=deployed.sandbox_uid,
                command_id=interrupted_command_id,
                text="Start a long-running executor command and keep this turn active.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert interrupted_input.dispatch.command_id == interrupted_command_id
        await _wait_for_setup(deployed, interrupted_session_id)
        interrupted_request = await model.request()
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
        await _wait_for_execution_progress(
            deployed,
            interrupted_session_id,
            event_pb2.ExecutionProgress.KIND_CELL,
            event_pb2.ExecutionProgress.PHASE_LIVE,
        )
        host_pid_file, host_pid = await _sampler_code_mode_host_pid(cluster, deployed)
        # A yielded cell returns control to sampling. Hold that continuation explicitly so
        # B's request is the next scripted exchange and A remains active until interruption.
        continuation = await model.request()
        assert continuation.native.client_metadata.session_id == interrupted_session_id
        await model.hold(continuation)

        created = await deployed.dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(
                session_id=session_id,
                spec=session_spec,
                setup_script="printf 'prepared-on-executor\\n' > prepared.txt\nprintf 'setup-on-executor\\n'\n",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert created.session.placement.central_sampler_v1.executor_environment.uid == deployed.sandbox_uid
        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id, expected_sandbox_uid=deployed.sandbox_uid, command_id=command_id, text=prompt
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
            request = await model.request()
        except Exception:
            await _capture_session_diagnostics(
                cluster, deployed, session_id, command_id, admission, phase="setup_or_initial_model_request"
            )
            raise
        assert request.model == MODEL
        assert request.native.client_metadata.session_id == session_id
        assert prompt in request.user_texts
        tool_command = (
            "set -eu; sleep 10; cat prepared.txt; printf 'tool-on-executor\\n' | tee result.txt; "
            "printf 'CENTRAL_EXECUTOR_OK\\n'"
        )
        try:
            await model.reply(request, Reasoning(reasoning), ShellCall("read-prepared-marker", tool_command))
            # Require the executor process to be running before interrupting Session A; B's shell
            # command sleeps for ten seconds so it can finish and return its real-executor output.
            await _wait_for_execution_progress(
                deployed,
                session_id,
                event_pb2.ExecutionProgress.KIND_PROCESS,
                event_pb2.ExecutionProgress.PHASE_RUNNING,
            )
            assert await _read_code_mode_host_pid(host_pid_file) == host_pid
            interruption = await deployed.dispatcher.InterruptTurn(
                session_dispatcher_pb2.InterruptTurnRequest(
                    command_id=str(uuid4()),
                    session_id=interrupted_session_id,
                    expected_sandbox_uid=deployed.sandbox_uid,
                    turn_id=interrupted_command_id,
                ),
                metadata=deployed.metadata,
                timeout=15,
            )
            assert interruption.dispatch.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
            await _wait_for_turn_status(
                deployed, interrupted_session_id, interrupted_command_id, event_pb2.TURN_STATUS_INTERRUPTED
            )
            assert await _read_code_mode_host_pid(host_pid_file) == host_pid
            request = await model.request()
            (tool_output,) = request.tool_outputs
            output = "\n".join(tool_output.blocks)
            assert "CENTRAL_EXECUTOR_OK" in output
            _verify_pod_executor(output)
            assert any(item.encrypted_content for item in request.native.reasoning)
            await model.reply(request, Text(answer))
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
        assert snapshot.executor.target.uid == deployed.sandbox_uid
        assert snapshot.expected_incarnation_uid == deployed.incarnation_uid
        assert snapshot.attached.setup_state == session_pb2.SETUP_STATE_SUCCEEDED
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
        assert all(entity.session_id == session_id for entity in entities.entities)
        for text in (reasoning, answer):
            (item,) = [entry for entry in completed if entry.event.item_completed.text == text]
            (entity,) = [
                entity
                for entity in entities.entities
                if entity.HasField("item") and entity.entity_id == item.event.item_completed.item_id
            ]
            assert entity.item.HasField("text")
            assert entity.item.HasField("text_ref")
        for kind in (event_pb2.ITEM_KIND_REASONING, event_pb2.ITEM_KIND_ASSISTANT_TEXT, event_pb2.ITEM_KIND_TOOL_CALL):
            assert any(entity.HasField("item") and entity.item.kind == kind for entity in entities.entities)
        assert any(
            entity.HasField("confirmed_input") and command_id in entity.confirmed_input.origin_command_ids
            for entity in entities.entities
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
                    "control_target": deployed.control_target,
                    "source_id": terminal.origin.source_id,
                    "terminal_cursor": terminal.cursor,
                    "folded_through": summary.folded_through,
                    "setup_state": session_pb2.SetupState.Name(snapshot.attached.setup_state),
                    "passed": True,
                },
                indent=2,
            )
            + "\n"
        )


async def pod_exec_session_rebinds_after_sandbox_suspend_resume(
    cluster: Cluster, client: Client, tmp_path: Path, scripted: ScriptedUpstream, executor: Executor
) -> None:
    """A resident central Session continues its next requested exec on the resumed Pod."""
    session_id = str(uuid4())
    first_command_id, resumed_command_id = str(uuid4()), str(uuid4())
    cwd = executor.cwd
    async with central_stack(cluster, tmp_path, template=executor.template) as deployed:
        target = identity_pb2.EnvironmentTarget(
            namespace=cluster.namespace, name=deployed.sandbox, uid=deployed.sandbox_uid
        )
        model = scripted.model(deployed.sampler_pod_name, session_pb2.HARNESS_CODEX)
        assert isinstance(model, CodexModel)
        session_spec = session_pb2.SessionSpec(
            placement=session_pb2.SessionPlacement(
                central_sampler_v1=session_pb2.CentralSamplerV1Placement(
                    executor_environment=session_pb2.ExactEnvironment(name=deployed.sandbox, uid=deployed.sandbox_uid)
                )
            ),
            cwd=cwd,
            model=MODEL,
        )
        created = await deployed.dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(session_id=session_id, spec=session_spec, setup_script=":\n"),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert created.session.placement.central_sampler_v1.executor_environment.uid == deployed.sandbox_uid
        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                expected_sandbox_uid=deployed.sandbox_uid,
                command_id=first_command_id,
                text="Start a process that records one marker and remains running.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == first_command_id
        await _wait_for_setup(deployed, session_id)

        request = await model.request()
        assert request.native.client_metadata.session_id == session_id
        assert request.native.code_mode
        first_cell = (
            'const started = await tools.exec_command({cmd: "echo before-resume >> incarnation.txt; sleep 120", '
            "yield_time_ms: 1000});\n"
            'if (started.session_id === undefined) throw new Error("expected a live process handle");\n'
            'store("pre_resume_process", started.session_id);\n'
            "text(JSON.stringify({session_id: started.session_id, output: started.output}));"
        )
        async with request._exchange as exchange:
            await exchange.send(
                *responses_sse.response_stream(
                    [
                        responses_sse.Reasoning(
                            "I will start the pre-resume process and keep its handle.",
                            "test-opaque-pre-resume-reasoning",
                        ),
                        responses_sse.CustomToolCall("start-pre-resume-process", "exec", first_cell),
                    ],
                    model=MODEL,
                ).events
            )
        request = await model.request()
        assert request.native.client_metadata.session_id == session_id
        assert any(output.call_id == "start-pre-resume-process" for output in request.tool_outputs)
        await model.reply(request, Text("The pre-resume process is running."))
        await _wait_for_turn_status(deployed, session_id, first_command_id, event_pb2.TURN_STATUS_COMPLETED)
        await _wait_for_execution_progress(
            deployed, session_id, event_pb2.ExecutionProgress.KIND_PROCESS, event_pb2.ExecutionProgress.PHASE_RUNNING
        )

        previous_incarnation = deployed.incarnation_uid
        await client.suspend_sandbox(target)
        async with asyncio.timeout(SANDBOX_READY_SECONDS):
            while True:
                suspended = await client.get_environment(target)
                if (
                    suspended.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_SUSPENDED
                    and not suspended.kubernetes_diagnostics.HasField("pod")
                ):
                    break
                await asyncio.sleep(0.2)

        await client.resume_sandbox(target)
        resumed = await wait_until_running(client, target)
        assert resumed.target.uid == deployed.sandbox_uid
        assert resumed.kubernetes_diagnostics.HasField("pod")
        assert resumed.kubernetes_diagnostics.pod.uid != previous_incarnation

        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                expected_sandbox_uid=deployed.sandbox_uid,
                command_id=resumed_command_id,
                text="Run a new exec command and report the marker file contents.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == resumed_command_id
        request = await model.request()
        assert request.native.client_metadata.session_id == session_id
        assert "Run a new exec command" in "\n".join(request.user_texts)
        resumed_cell = (
            'const result = await tools.exec_command({cmd: "echo after-resume >> incarnation.txt; sleep 2; cat incarnation.txt", '
            "yield_time_ms: 10000});\n"
            "text(JSON.stringify({exit_code: result.exit_code, output: result.output}));"
        )
        async with request._exchange as exchange:
            await exchange.send(
                *responses_sse.response_stream(
                    [
                        responses_sse.Reasoning(
                            "I will run a fresh command on the resumed executor.", "test-opaque-resumed-exec-reasoning"
                        ),
                        responses_sse.CustomToolCall("run-resumed-process", "exec", resumed_cell),
                    ],
                    model=MODEL,
                ).events
            )
        request = await model.request()
        assert request.native.client_metadata.session_id == session_id
        resumed_tool_outputs = [output for output in request.tool_outputs if output.call_id == "run-resumed-process"]
        await model.reply(request, Text("The resumed executor retained the workspace and completed the new command."))
        await _wait_for_turn_status(deployed, session_id, resumed_command_id, event_pb2.TURN_STATUS_COMPLETED)

        snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        assert len(resumed_tool_outputs) == 2, (
            "expected the exec result followed by the Sandbox incarnation notice; "
            f"expected_incarnation={snapshot.expected_incarnation_uid!r}, "
            f"resumed_incarnation={resumed.kubernetes_diagnostics.pod.uid!r}, blocked={snapshot.blocked}, "
            f"tool_outputs={resumed_tool_outputs!r}"
        )
        exec_output, notice_output = resumed_tool_outputs
        exec_output_text = "\n".join(exec_output.blocks)
        notice_output_text = "\n".join(notice_output.blocks)
        assert exec_output_text.startswith("Script completed\n"), exec_output_text
        _, separator, result_json = exec_output_text.partition("Output:\n")
        assert separator, f"exec result omitted the output envelope: {exec_output_text!r}"
        assert notice_output_text.startswith("The execution Sandbox changed incarnation"), notice_output_text
        result = json.loads(result_json.strip())
        assert result["exit_code"] == 0, result
        assert result["output"].splitlines() == ["before-resume", "after-resume"]

        assert snapshot.executor.target.uid == deployed.sandbox_uid
        assert snapshot.expected_incarnation_uid == resumed.kubernetes_diagnostics.pod.uid
        assert not snapshot.blocked
        (undeclared_outputs_dir() / "central-sampler-pod-resume.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "sandbox": deployed.sandbox,
                    "sandbox_uid": deployed.sandbox_uid,
                    "previous_incarnation_uid": previous_incarnation,
                    "resumed_incarnation_uid": resumed.kubernetes_diagnostics.pod.uid,
                    "first_command_id": first_command_id,
                    "resumed_command_id": resumed_command_id,
                    "model_visible_notice": "The execution Sandbox changed incarnation",
                    "workspace_output": result["output"],
                    "passed": True,
                },
                indent=2,
            )
            + "\n"
        )


# gazelle:include_dep @pypi//protobuf


async def _wait_for_history_fold(
    deployed: CentralStack, session_id: str, cursor: int
) -> history_service_pb2.SessionSummary:
    async with asyncio.timeout(60), aclosing(deployed.history.watch_session_summaries()) as changes:
        async for _ in changes:
            summary = await deployed.history.get_session_summary(session_id)
            assert summary.feed_status != history_service_pb2.FEED_STATUS_FAILED, summary
            if summary.folded_through >= cursor:
                return summary
    raise AssertionError(f"History watch ended before folding {session_id=} through {cursor=}")


async def sampler_roll_retains_active_turn_and_history_accepts_next_input(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream, executor: Executor
) -> None:
    """Lost JavaScript work is settled once before the successor continues the confirmed input."""
    session_id, command_id, next_command_id = str(uuid4()), str(uuid4()), str(uuid4())
    async with central_stack(cluster, tmp_path, template=executor.template) as deployed:
        model = scripted.model(deployed.sampler_pod_name, session_pb2.HARNESS_CODEX)
        assert isinstance(model, CodexModel)
        await deployed.dispatcher.CreateSession(
            session_dispatcher_pb2.CreateSessionRequest(
                session_id=session_id,
                spec=session_pb2.SessionSpec(
                    placement=session_pb2.SessionPlacement(
                        central_sampler_v1=session_pb2.CentralSamplerV1Placement(
                            executor_environment=session_pb2.ExactEnvironment(
                                name=deployed.sandbox, uid=deployed.sandbox_uid
                            )
                        )
                    ),
                    cwd=executor.cwd,
                    model=MODEL,
                ),
                setup_script=":\n",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                expected_sandbox_uid=deployed.sandbox_uid,
                command_id=command_id,
                text="Start an executor process and keep this turn active.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == command_id
        await _wait_for_setup(deployed, session_id)
        request = await model.request()
        assert request.native.client_metadata.session_id == session_id
        async with request._exchange as exchange:
            await exchange.send(
                *responses_sse.response_stream(
                    [
                        responses_sse.CustomToolCall(
                            "sampler-roll-live-cell",
                            "exec",
                            '// @exec: {"yield_time_ms": 1}\n'
                            'let result = await tools.exec_command({cmd: "echo started >> roll-effects.txt; sleep 300", '
                            "yield_time_ms: 1});\n"
                            "while (result.session_id !== undefined) {\n"
                            "  result = await tools.write_stdin({session_id: result.session_id, yield_time_ms: 300000});\n"
                            "}\n"
                            "text(result.output);",
                        )
                    ],
                    model=MODEL,
                ).events
            )
        for kind, phase in (
            (event_pb2.ExecutionProgress.KIND_CELL, event_pb2.ExecutionProgress.PHASE_LIVE),
            (event_pb2.ExecutionProgress.KIND_PROCESS, event_pb2.ExecutionProgress.PHASE_RUNNING),
        ):
            await _wait_for_execution_progress(deployed, session_id, kind, phase)
        continuation = await model.request()
        assert continuation.native.client_metadata.session_id == session_id
        await model.hold(continuation)

        snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        assert snapshot.attached.active_turn_id == command_id
        summary = await _wait_for_history_fold(deployed, session_id, snapshot.attached.last_cursor)
        assert summary.attached.active_turn_id == command_id
        prefix = await deployed.history.read_session_events(session_id, limit=1000)
        (confirmed,) = [
            entry
            for entry in prefix.entries
            if entry.event.HasField("harness_user_message_confirmed")
            and command_id in entry.event.harness_user_message_confirmed.origin_command_ids
        ]
        (started,) = [entry for entry in prefix.entries if entry.event.HasField("turn_started")]
        assert confirmed.cursor < started.cursor
        gateway = json.loads(
            await cluster.kubectl("get", "pods", "-l", "app.kubernetes.io/name=runner-gateway", "-o", "json")
        )
        gateway_uids = {pod["metadata"]["uid"] for pod in gateway["items"]}
        sampler = json.loads(await cluster.kubectl("get", "pod", deployed.sampler_pod_name, "-o", "json"))
        await capture_sampler_diagnostics(cluster)
        # An abrupt replacement loses the shared JavaScript host while the executor remains alive.
        await cluster.kubectl("delete", "pod", deployed.sampler_pod_name, "--grace-period=0", "--force", "--wait=true")
        await cluster.kubectl("rollout", "status", "deployment/central-sampler", "--timeout=120s")
        pods = json.loads(
            await cluster.kubectl("get", "pods", "-l", "app.kubernetes.io/name=central-sampler", "-o", "json")
        )
        (successor,) = [pod for pod in pods["items"] if not pod["metadata"].get("deletionTimestamp")]
        assert successor["metadata"]["uid"] != sampler["metadata"]["uid"]
        successor_model = scripted.model(successor["metadata"]["name"], session_pb2.HARNESS_CODEX)
        assert isinstance(successor_model, CodexModel)
        # The former owner's real 60-second lease can outlive Pod readiness.
        async with asyncio.timeout(90):
            request = await successor_model.request()
        assert request.native.client_metadata.session_id == session_id
        await successor_model.reply(request, Text("The sampler changed; the earlier process outcome is unknown."))
        await _wait_for_turn_status(deployed, session_id, command_id, event_pb2.TURN_STATUS_COMPLETED)

        # History can lag the sampler; its fold cursor must reach the sampler's terminal event.
        frames = deployed.sampler.FollowEvents(
            sampler_pb2.FollowEventsRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        entries = [frame.entry async for frame in frames]
        terminal = next(entry for entry in entries if entry.event.HasField("turn_completed"))
        summary = await _wait_for_history_fold(deployed, session_id, terminal.cursor)
        assert summary.feed_status == history_service_pb2.FEED_STATUS_ACTIVE
        assert not summary.attached.active_turn_id
        assert summary.last_turn_status == event_pb2.TURN_STATUS_COMPLETED
        retained = await deployed.history.read_session_events(session_id, limit=1000)
        assert list(retained.entries[: len(prefix.entries)]) == list(prefix.entries)
        assert [entry.cursor for entry in retained.entries] == sorted({entry.cursor for entry in retained.entries})
        assert len({(entry.origin.source_id, entry.origin.sequence) for entry in retained.entries}) == len(
            retained.entries
        )
        assert not [entry for entry in retained.entries if entry.event.HasField("command_failed")]
        assert len([entry for entry in retained.entries if entry.event.HasField("turn_completed")]) == 1
        loss = [
            entry
            for entry in retained.entries
            if entry.event.HasField("execution_progress")
            and entry.event.execution_progress.kind == event_pb2.ExecutionProgress.KIND_CODE_HOST_LOSS
            and entry.event.execution_progress.phase == event_pb2.ExecutionProgress.PHASE_COMMITTED
        ]
        (reconciled,) = loss
        for kind in (event_pb2.ExecutionProgress.KIND_CELL, event_pb2.ExecutionProgress.KIND_PROCESS):
            (abandoned,) = [
                entry
                for entry in retained.entries
                if entry.event.HasField("execution_progress")
                and entry.event.execution_progress.kind == kind
                and entry.event.execution_progress.phase == event_pb2.ExecutionProgress.PHASE_ABANDONED
            ]
            assert prefix.entries[-1].cursor < abandoned.cursor < reconciled.cursor < terminal.cursor

        submitted = await deployed.dispatcher.SubmitInput(
            session_dispatcher_pb2.SubmitInputRequest(
                session_id=session_id,
                expected_sandbox_uid=deployed.sandbox_uid,
                command_id=next_command_id,
                text="Read the process marker without starting the earlier command again.",
            ),
            metadata=deployed.metadata,
            timeout=15,
        )
        assert submitted.dispatch.command_id == next_command_id
        request = await successor_model.request()
        assert request.native.client_metadata.session_id == session_id
        await successor_model.reply(request, ShellCall("read-roll-effects", "cat roll-effects.txt"))
        request = await successor_model.request()
        (output,) = [output for output in request.tool_outputs if output.call_id == "read-roll-effects"]
        output_text = "\n".join(output.blocks)
        _, separator, process_output = output_text.partition("Output:\n")
        assert separator, output_text
        assert process_output.strip() == "started", process_output
        await successor_model.reply(request, Text("The next input completed on the same executor."))
        await _wait_for_turn_status(deployed, session_id, next_command_id, event_pb2.TURN_STATUS_COMPLETED)
        frames = deployed.sampler.FollowEvents(
            sampler_pb2.FollowEventsRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        entries = [frame.entry async for frame in frames]
        summary = await _wait_for_history_fold(deployed, session_id, entries[-1].cursor)
        assert summary.feed_status == history_service_pb2.FEED_STATUS_ACTIVE
        assert not summary.attached.active_turn_id
        retained = await deployed.history.read_session_events(session_id, limit=1000)
        assert not [entry for entry in retained.entries if entry.event.HasField("command_failed")]
        assert [
            entry.event.turn_completed.turn_id for entry in retained.entries if entry.event.HasField("turn_completed")
        ] == [command_id, next_command_id]
        assert any(
            entry.event.HasField("harness_user_message_confirmed")
            and next_command_id in entry.event.harness_user_message_confirmed.origin_command_ids
            for entry in retained.entries
        )
        entities = await deployed.history.read_thread_entities(
            history_service_pb2.ReadThreadEntitiesRequest(
                session_id=session_id, range=history_service_pb2.EntityRange(limit=200)
            )
        )
        assert any(
            entity.HasField("confirmed_input") and next_command_id in entity.confirmed_input.origin_command_ids
            for entity in entities.entities
        )
        for settled_command_id in (command_id, next_command_id):
            command = await deployed.sampler.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=session_id, command_id=settled_command_id),
                metadata=deployed.sampler_metadata,
                timeout=10,
            )
            assert command.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED
        snapshot = await deployed.sampler.GetSession(
            sampler_pb2.GetSessionRequest(session_id=session_id), metadata=deployed.sampler_metadata, timeout=10
        )
        assert not snapshot.blocked
        assert not snapshot.attached.active_turn_id
        assert snapshot.expected_incarnation_uid == deployed.incarnation_uid
        sandbox = json.loads(await cluster.kubectl("get", "sandbox", deployed.sandbox, "-o", "json"))
        assert sandbox["status"]["incarnation"]["uid"] == deployed.incarnation_uid
        gateway_after = json.loads(
            await cluster.kubectl("get", "pods", "-l", "app.kubernetes.io/name=runner-gateway", "-o", "json")
        )
        assert {pod["metadata"]["uid"] for pod in gateway_after["items"]} == gateway_uids
        (undeclared_outputs_dir() / "central-sampler-roll.json").write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "command_id": command_id,
                    "next_command_id": next_command_id,
                    "previous_sampler_uid": sampler["metadata"]["uid"],
                    "successor_sampler_uid": successor["metadata"]["uid"],
                    "executor_incarnation_uid": deployed.incarnation_uid,
                    "retained_prefix_cursor": prefix.entries[-1].cursor,
                    "host_loss_cursor": reconciled.cursor,
                    "first_terminal_cursor": terminal.cursor,
                    "folded_through": summary.folded_through,
                    "feed_status": history_service_pb2.FeedStatus.Name(summary.feed_status),
                    "command_failed_count": 0,
                    "passed": True,
                },
                indent=2,
            )
            + "\n"
        )
