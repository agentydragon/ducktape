"""Dispatcher admission through a real k3s Sandbox executor to retained central-sampler History."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError
from agentplane.history_service import history_service_pb2
from agentplane.native.codex.testing.responses_sse import CustomToolCall, Message, Reasoning, response_stream
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2
from agentplane.system_tests.central_sampler import (
    MODEL,
    PROVIDER,
    SERVICE,
    CentralStack,
    capture_sampler_diagnostics,
    central_stack,
)
from agentplane.system_tests.cluster import kubectl
from agentplane.system_tests.conftest import POD_TEMPLATE, VM_TEMPLATE, VM_WORKING_DIRECTORY
from agentplane.system_tests.k3s.stack import node_ip
from agentplane.x.central_sampler import sampler_pb2
from agentplane.x.central_sampler.testing import Endpoint
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def _capture_session_diagnostics(
    deployed: CentralStack, session_id: str, command_id: str, admission: str, *, phase: str
) -> None:
    """Retain sampler, History, Sandbox, and full event evidence for a Session."""
    diagnostic: dict[str, object] = {
        "phase": phase,
        "session_id": session_id,
        "command_id": command_id,
        "dispatcher_admission": admission,
    }
    try:
        pod_artifacts = await capture_sampler_diagnostics()
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
        sandbox_state = json.loads(await kubectl("get", "sandbox", deployed.sandbox, "-o", "json"))
        diagnostic["sandbox"] = {
            "name": sandbox_state["metadata"]["name"],
            "uid": sandbox_state["metadata"]["uid"],
            "status": sandbox_state.get("status", {}),
        }
    except Exception as error:
        diagnostic["sandbox_error"] = repr(error)
    try:
        runner_lease = await kubectl("get", "lease", f"{deployed.sandbox}-runner", "-o", "json")
        diagnostic["runner_lease"] = json.loads(runner_lease)
    except Exception as error:
        diagnostic["runner_lease_error"] = repr(error)
    try:
        diagnostic["cluster_services_endpoints_pods"] = json.loads(
            await kubectl("get", "services,endpointslices,pods", "-o", "json")
        )
    except Exception as error:
        diagnostic["cluster_services_endpoints_pods_error"] = repr(error)
    for component in ("runner-gateway", "history-service"):
        try:
            diagnostic[f"{component}_logs"] = await kubectl(
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
            retained = await deployed.history.read_session_events(session_id, limit=1000)
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


async def _central_sampler_scenario(tmp_path: Path, *, template: str, cwd: str) -> None:
    session_id, command_id = str(uuid4()), str(uuid4())
    prompt = "Read the setup marker and report the result."
    reasoning = "I will read the marker created during setup."
    answer = "The executor returned prepared-on-executor."
    is_vm = template == VM_TEMPLATE
    vm_execution: dict[str, str | int] | None = None
    async with (
        Endpoint(bind_host=node_ip()) as endpoint,
        central_stack(tmp_path, endpoint.origin, template=template) as deployed,
    ):
        opening = await deployed.dispatcher.OpenSession(
            session_dispatcher_pb2.OpenSessionRequest(
                session_id=session_id,
                sandbox=deployed.sandbox,
                spec=runner_pb2.SessionSpec(
                    harness=runner_pb2.HARNESS_CENTRAL_SAMPLER,
                    cwd=cwd,
                    model=MODEL,
                    central_sampler=runner_pb2.CentralSamplerConfig(service=SERVICE, provider=PROVIDER),
                ),
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
            first_exchange = await endpoint.next()
        except Exception:
            await _capture_session_diagnostics(
                deployed, session_id, command_id, admission, phase="setup_or_initial_model_request"
            )
            raise
        async with first_exchange as exchange:
            assert any(item.get("content") == prompt for item in exchange.request.body["input"])
            assert exchange.request.body["client_metadata"]["session_id"] == session_id
            tool_command = "set -eu; cat prepared.txt; printf tool-on-executor > result.txt"
            if is_vm:
                tool_command = (
                    "set -eu; printf 'VM_PID1 %s\\n' \"$(cat /proc/1/comm)\"; "
                    "printf 'VM_EXECUTOR_UID %s\\n' \"$(id -u)\"; "
                    "printf 'VM_EXECUTOR_CGROUP %s\\n' \"$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)\"; "
                    "cat prepared.txt; printf tool-on-executor > result.txt"
                )
            await exchange.send(
                *response_stream(
                    [
                        Reasoning(reasoning, "test-opaque-central-reasoning"),
                        CustomToolCall(
                            "read-prepared-marker",
                            "exec",
                            f"text(await tools.exec_command({json.dumps({'cmd': tool_command})}));",
                        ),
                    ],
                    model=MODEL,
                ).events
            )
        try:
            async with await endpoint.next() as exchange:
                outputs = [item for item in exchange.request.body["input"] if item["type"] == "custom_tool_call_output"]
                result = json.loads(outputs[-1]["output"][0]["text"])
                assert result["exit_code"] == 0
                if is_vm:
                    assert "prepared-on-executor\n" in result["output"]
                    assert "VM_PID1 systemd\n" in result["output"]
                    assert "VM_EXECUTOR_UID 107\n" in result["output"]
                    (cgroup_line,) = [
                        line for line in result["output"].splitlines() if line.startswith("VM_EXECUTOR_CGROUP ")
                    ]
                    executor_cgroup = cgroup_line.removeprefix("VM_EXECUTOR_CGROUP ")
                    assert executor_cgroup.startswith(
                        "/agentplane.slice/agentplane-executors.slice/agentplane-native-"
                    ), executor_cgroup
                    assert executor_cgroup.count("/") == 3, executor_cgroup
                    assert executor_cgroup.endswith(".service"), executor_cgroup
                    vm_execution = {"pid1": "systemd", "uid": 107, "executor_cgroup": executor_cgroup}
                else:
                    assert result["output"] == "prepared-on-executor\n"
                assert any(
                    item.get("encrypted_content") == "test-opaque-central-reasoning"
                    for item in exchange.request.body["input"]
                )
                await exchange.send(*response_stream([Message(answer)], model=MODEL).events)
        except Exception as error:
            try:
                await _capture_session_diagnostics(
                    deployed, session_id, command_id, admission, phase="tool_result_model_request"
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
                deployed, session_id, command_id, admission, phase="setup_stdout_assertion"
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
                    "tool_output": result["output"],
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


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_dispatcher_central_setup_tool_and_retained_answer(k3s_deployment: None, tmp_path: Path) -> None:
    del k3s_deployment  # This scenario is only valid on its own disposable stack, never against staging.
    await _central_sampler_scenario(tmp_path, template=POD_TEMPLATE, cwd="/state/workspaces/central-acceptance")


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_dispatcher_central_setup_tool_and_retained_answer_vm(k3s_deployment: None, tmp_path: Path) -> None:
    del k3s_deployment  # This scenario is only valid on its own disposable stack, never against staging.
    await _central_sampler_scenario(tmp_path, template=VM_TEMPLATE, cwd=VM_WORKING_DIRECTORY)


if __name__ == "__main__":
    pytest_bazel.main()

# gazelle:include_dep @pypi//protobuf
