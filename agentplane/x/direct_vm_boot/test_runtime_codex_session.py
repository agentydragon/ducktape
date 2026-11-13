"""Drive a real Dispatcher Session through the packaged guest Codex harness and executor."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import grpc
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToJson

from agentplane.env.config import (
    Codex,
    Configuration,
    Endpoint,
    EnvironmentRuntimeControl,
    EnvironmentVariables,
    SVIDEndpoint,
    VMEnvironment,
    VMExecutor,
)
from agentplane.env.e2e.testing import events, launches
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.native.codex import wire
from agentplane.native.codex.testing import frames, model_catalog, responses_sse
from agentplane.native.codex.testing.model import MODEL as CODEX_MODEL
from agentplane.native.codex.testing.requests import ResponsesRequest
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.net.peertls.config import EGRESS_PROXY_URI, HISTORY_SERVICE_URI, NOTIFICATIONS_URI
from agentplane.proto import event_log_pb2, event_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.system_tests.vm.configuration import CPU_QUOTA_PERIOD_MICROS, RESOURCES
from agentplane.system_tests.vm.guest_supervision_probe import VM_SUPERVISION_PROBE, assert_vm_supervision_probe
from agentplane.testing.codex_model import CodexModel
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.scripted_model import ModelRequest, Text
from agentplane.x.direct_vm_boot.platform_services import (
    CLI_PLATFORM_PROBE,
    assert_credential_rotation_refuses_old_svid,
    assert_platform_cli_output,
    platform_services,
)
from agentplane.x.direct_vm_boot.runtime_guest import (
    authenticated_readiness,
    gateway_address,
    guest_image_bind_mounts,
    runtime_guest_container,
    sandbox_with_runtime_gateway,
    write_guest_inputs,
)
from agentplane.x.direct_vm_boot.session_helpers import (
    assert_guest_setup_is_supervised,
    history_turn_completed,
    wait_for_session_ready,
)
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep //agentplane/testing:native_gateway_fixtures
# gazelle:include_dep //agentplane/history_service/testing:fixtures
pytest_plugins = ("agentplane.testing.native_gateway_fixtures", "agentplane.history_service.testing.fixtures")


@pytest.fixture
async def sandbox(api_server: KubernetesApiServer) -> AsyncIterator[SandboxCluster]:
    async with sandbox_with_runtime_gateway(api_server) as started:
        yield started


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=(sandbox.namespace,),
        svid_ca=gateway_service.environment_runtime_ca,
        policy_namespace=sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
async def ingress(history_service: GoHistoryService) -> AsyncIterator[Endpoint]:
    async with history_tunnel(
        history_service.ingress_target, listen_host="0.0.0.0", advertised_host="10.0.2.1"
    ) as proxy:
        yield Endpoint(
            target=history_service.ingress_target,
            svid=SVIDEndpoint(server_uri=HISTORY_SERVICE_URI, proxy_address=proxy),
        )


@pytest.fixture
async def endpoint() -> AsyncIterator[OpenAIResponses]:
    server = OpenAIResponses(bind_host="0.0.0.0")
    server.serve_catalog([model_catalog.bundled("gpt-6-luna", slug=CODEX_MODEL)])
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def model(endpoint: OpenAIResponses) -> CodexModel:
    return CodexModel(endpoint)


async def _model_request(model: CodexModel) -> ModelRequest[ResponsesRequest]:
    request = await model.request()
    with (undeclared_outputs_dir() / "codex-model-requests.jsonl").open("a") as log:
        log.write(request.native.model_dump_json() + "\n")
    return request


async def _record_history(history: HistoryServiceClient, session_id: str) -> None:
    try:
        async with asyncio.timeout(5):
            response = await history.read_session_events(session_id, after_cursor=0, limit=1000)
    except (ServiceError, TimeoutError) as error:
        (undeclared_outputs_dir() / "codex-history-error.txt").write_text(str(error))
    else:
        (undeclared_outputs_dir() / "codex-history.json").write_text(
            MessageToJson(response, preserving_proto_field_name=True)
        )


async def _send_response(
    model: CodexModel, request: ModelRequest[ResponsesRequest], *items: responses_sse.Item
) -> None:
    # The code-mode fixture is intentionally scripted at the native Responses boundary so the test
    # can capture the actual exec result JSON emitted by the guest tool.
    async with request._exchange as exchange:
        await exchange.send(*responses_sse.response_stream(list(items), model=model.model).events)


async def _run_shell_turn(
    *,
    dispatcher: SessionDispatcherClient,
    history: HistoryServiceClient,
    model: CodexModel,
    sandbox: SandboxCluster,
    session_id: str,
    input_text: str,
    command_id: str,
    call_id: str,
    shell_command: str,
    expected_output: str,
    final_text: str,
) -> tuple[list[event_log_pb2.EventEntry], str]:
    try:
        dispatch = await dispatcher.submit_input(session_id, command_id, sandbox.name, sandbox.uid, input_text)
        receipt = (await admitted(dispatcher, dispatch)).admission
        request = await _model_request(model)
        assert request.user_texts[-1] == input_text
        assert request.native.code_mode

        script = (
            '// @exec: {"yield_time_ms": 1}\n'
            f"let result = await tools.exec_command({json.dumps({'cmd': shell_command, 'yield_time_ms': 1000})});\n"
            "while (result.session_id != null) {\n"
            "  const next = await tools.write_stdin({session_id: result.session_id, chars: '', yield_time_ms: 1000});\n"
            "  result = {...next, output: result.output + next.output};\n"
            "}\n"
            "text('VM_EXEC_RESULT ' + JSON.stringify(result));"
        )
        await _send_response(model, request, responses_sse.CustomToolCall(call_id, "exec", script))

        output_call_id = call_id
        blocks: list[str] = []
        waits = 0
        while True:
            tool_request = await _model_request(model)
            (output,) = [output for output in tool_request.tool_outputs if output.call_id == output_call_id]
            blocks.extend(output.blocks)
            native_outputs = [
                output
                for output in (
                    *tool_request.native.custom_tool_call_outputs,
                    *tool_request.native.function_call_outputs,
                )
                if output.call_id == output_call_id
            ]
            assert len(native_outputs) == 1, tool_request.native.item_kinds
            running = re.search(r"^Script running with cell ID (\S+)", "\n".join(output.blocks), re.MULTILINE)
            if running is None:
                break
            waits += 1
            output_call_id = f"{call_id}-wait-{waits}"
            await _send_response(
                model,
                tool_request,
                responses_sse.FunctionCall(output_call_id, "wait", {"cell_id": running[1], "yield_time_ms": 1000}),
            )

        reports = [
            line.removeprefix("VM_EXEC_RESULT ")
            for block in blocks
            for line in block.splitlines()
            if line.startswith("VM_EXEC_RESULT ")
        ]
        assert len(reports) == 1, blocks
        result: dict[str, object] = json.loads(reports[0])
        assert result.get("error") is None, result
        assert result.get("isError", False) is False, result
        assert result.get("exit_code") == 0, result
        command_output = result.get("output")
        assert isinstance(command_output, str), result
        assert expected_output in command_output, result
        await model.reply(tool_request, Text(final_text))
        entries = await history_turn_completed(history, session_id, receipt.cursor - 1)

        (tool_item_id,) = [
            entry.event.item_started.item_id
            for entry in events.of_kind(entries, "item_started")
            if entry.event.item_started.tool_name == "commandExecution"
        ]
        completed = events.completed(entries, tool_item_id)
        assert completed.WhichOneof("outcome") == "tool", completed
        assert completed.tool.succeeded, completed
        assert expected_output in completed.tool.output, completed
        arguments = json.loads(events.tool_arguments(entries, tool_item_id))
        assert shlex.split(arguments["command"])[-1] == shell_command, arguments
        native_frames = [
            json.loads(entry.event.native.line)
            for entry in events.of_kind(entries, "native")
            if entry.event.native.direction == event_pb2.DIRECTION_FROM_HARNESS
        ]
        (execution,) = frames.assert_item_lifecycles(native_frames, wire.CommandExecutionItem)
        assert execution.id == tool_item_id
        assert execution.status is wire.CommandExecutionStatus.COMPLETED
        assert execution.exit_code == 0, execution
        return entries, command_output
    finally:
        await _record_history(history, session_id)


async def test_packaged_codex_session_runs_isolated_shell_and_accepts_next_input(
    tmp_path: Path,
    postgres_admin_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    control: EnvironmentRuntimeControl,
    gateway_service: GatewayProcess,
    client_token: Path,
    dispatcher: SessionDispatcherClient,
    history_service: GoHistoryService,
    ingress: Endpoint,
    endpoint: OpenAIResponses,
    model: CodexModel,
) -> None:
    with pytest.raises(ServiceError) as history_reader:
        await history_service.reader.get_session_summary(str(uuid4()))
    assert history_reader.value.code == grpc.StatusCode.NOT_FOUND
    await history_service.process.health(serving=True)

    inputs = tmp_path / "inputs"
    state = tmp_path / "state"
    outputs = tmp_path / "outputs"
    inputs.mkdir()
    state.mkdir()
    outputs.mkdir()
    guest_image_mounts = guest_image_bind_mounts(inputs)

    bypass_executor_control = os.environ.get("DIRECT_VM_CODEX_BYPASS_EXECUTOR_CONTROL") == "1"
    executor_resources = RESOURCES.executor
    assert executor_resources is not None
    hostfwd_address = gateway_address(sandbox.uid)
    container_name = f"agentplane-runtime-codex-session-{UUID(sandbox.uid).hex[:16]}"
    requested_command_ids = (str(uuid4()), str(uuid4()))
    assert len(set(requested_command_ids)) == 2
    async with (
        platform_services(
            postgres_admin_url=postgres_admin_url,
            sandbox=sandbox,
            ca=gateway_service.environment_runtime_ca,
            directory=tmp_path / "platform",
            notifications_server_uri=(
                EGRESS_PROXY_URI
                if os.environ.get("DIRECT_VM_NOTIFICATION_WRONG_SERVER_IDENTITY_CONTROL") == "1"
                else NOTIFICATIONS_URI
            ),
        ) as platform,
        model_config(listen_host="0.0.0.0", advertised_host="10.0.2.1") as model_config_target,
    ):
        config = Configuration(
            version=2,
            state_dir="/state",
            workspace_root="/state/workspaces",
            initialization_cwd="/state/workspaces",
            control=EnvironmentRuntimeControl(listen_address="0.0.0.0:9090", security=control.security),
            ingress=ingress,
            model_config_target=model_config_target,
            platform_instructions="Exercise the packaged Codex harness and isolated executor with a retained marker.",
            harness_environment=EnvironmentVariables(
                values={
                    "OPENAI_API_KEY": launches.TOKEN,
                    "AGENTPLANE_EGRESS_GRPC_URL": platform.guest_url,
                    "AGENTPLANE_NOTIFICATIONS_GRPC_URL": platform.guest_url,
                    "AGENTPLANE_EGRESS_GRPC_CREDENTIAL": "deliberately-forged-caller-token",
                    "AGENTPLANE_NOTIFICATIONS_GRPC_CREDENTIAL": "deliberately-forged-caller-token",
                    "HOME": "/state/home",
                    "NO_PROXY": "10.0.2.1,127.0.0.1,localhost",
                    "PATH": "/run/current-system/sw/bin:/run/agentplane/environment/bin",
                }
            ),
            codex=Codex(
                binary="/run/current-system/sw/bin/codex",
                base_url=f"http://10.0.2.1:{urlsplit(endpoint.origin).port}/v1",
                token_env="OPENAI_API_KEY",
            ),
            environment=VMEnvironment(
                resources=RESOURCES.model_copy(update={"executor": None}) if bypass_executor_control else RESOURCES,
                agent_uid=107,
                agent_gid=107,
                block_devices=("/dev/disk/by-id/virtio-workspace",),
                executor=None if bypass_executor_control else VMExecutor(url="ws://127.0.0.1:4501"),
            ),
        )
        write_guest_inputs(inputs, sandbox, config)
        async with runtime_guest_container(
            container_name=container_name,
            inputs=inputs,
            guest_image_mounts=guest_image_mounts,
            state=state,
            outputs=outputs,
            gateway_address=hostfwd_address,
        ) as docker_done:
            ready_task = asyncio.create_task(
                authenticated_readiness(gateway_service, client_token, api_server, sandbox)
            )
            try:
                done, _ = await asyncio.wait((docker_done, ready_task), return_when=asyncio.FIRST_COMPLETED)
                assert docker_done not in done, "QEMU guest exited before authenticated Runtime readiness"
                initial_status = await ready_task
            finally:
                if not ready_task.done():
                    ready_task.cancel()
                    await asyncio.gather(ready_task, return_exceptions=True)

            session_id = uuid4()
            workspace = Path("/state/workspaces") / session_id.hex
            spec = launches.spec(session_pb2.HARNESS_CODEX, workspace, sandbox.identity)
            await dispatcher.unary(
                dispatcher.stub.CreateSession,
                session_dispatcher_pb2.CreateSessionRequest(session_id=str(session_id), spec=spec),
            )
            await accepted_session(dispatcher, session_id)

            marker = f"direct-vm-codex-{session_id.hex}"
            marker_path = str(workspace / "session-marker.txt")
            cgroup_probe = (
                "cgroup=$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)\n"
                "printf 'VM_EXECUTOR_MEMORY_MAX %s\\n' "
                '"$(cat "/sys/fs/cgroup$cgroup/memory.max")"\n'
                "printf 'VM_EXECUTOR_CPU_MAX %s\\n' "
                '"$(cat "/sys/fs/cgroup$cgroup/cpu.max")"\n'
            )
            first_command = (
                f"set -eu\nprintf '%s\\n' '{marker}' > '{marker_path}' && cat '{marker_path}'\n"
                f"{VM_SUPERVISION_PROBE}\n{cgroup_probe}"
            )
            write_entries, first_output = await _run_shell_turn(
                dispatcher=dispatcher,
                history=history_service.reader,
                model=model,
                sandbox=sandbox,
                session_id=str(session_id),
                input_text="Create the marker file and report the marker.",
                command_id=requested_command_ids[0],
                call_id=f"call_write_{session_id.hex}",
                shell_command=first_command,
                expected_output=marker,
                final_text="MARKER_WRITTEN",
            )
            assert_vm_supervision_probe(first_output, slice_name="agentplane-executors.slice")
            memory_values = [
                line.removeprefix("VM_EXECUTOR_MEMORY_MAX ")
                for line in first_output.splitlines()
                if line.startswith("VM_EXECUTOR_MEMORY_MAX ")
            ]
            cpu_values = [
                line.removeprefix("VM_EXECUTOR_CPU_MAX ")
                for line in first_output.splitlines()
                if line.startswith("VM_EXECUTOR_CPU_MAX ")
            ]
            assert memory_values == [str(executor_resources.memory_max_bytes)], first_output
            assert cpu_values == [f"{executor_resources.cpu_quota_micros} {CPU_QUOTA_PERIOD_MICROS}"], first_output

            read_entries, platform_output = await _run_shell_turn(
                dispatcher=dispatcher,
                history=history_service.reader,
                model=model,
                sandbox=sandbox,
                session_id=str(session_id),
                input_text="Read the existing marker file and report its contents.",
                command_id=requested_command_ids[1],
                call_id=f"call_read_{session_id.hex}",
                shell_command=f"set -eu\ncat '{marker_path}'\n{CLI_PLATFORM_PROBE}",
                expected_output=marker,
                final_text="MARKER_READ",
            )
            assert write_entries
            assert read_entries
            assert_platform_cli_output(platform_output, platform, sandbox)
            await assert_guest_setup_is_supervised(dispatcher, history_service.reader, spec)
            assert not docker_done.done(), "QEMU guest exited before both Session turns completed"
            final_status = await wait_for_session_ready(gateway_service, client_token, sandbox, str(session_id))
            assert final_status.environment_runtime_boot_id == initial_status.environment_runtime_boot_id
        await assert_credential_rotation_refuses_old_svid(platform, sandbox)

    serial = (outputs / "runtime.serial.log").read_text(errors="replace")
    runtime_records = [line for line in serial.splitlines() if '"logger":"environment-runtime-lifecycle"' in line]
    assert runtime_records, "packaged Runtime did not emit lifecycle records on the guest serial console"
    history_entries = list(
        (await history_service.reader.read_session_events(str(session_id), after_cursor=0, limit=1000)).entries
    )
    events.assert_contiguous(history_entries)
    events.assert_sourced(history_entries)
    command_ids = [
        entry.event.command_admitted.command.command_id for entry in events.of_kind(history_entries, "command_admitted")
    ]
    turns = events.of_kind(history_entries, "turn_completed")
    assert command_ids == list(requested_command_ids)
    assert len(turns) == 2
    assert [entry.event.turn_completed.status for entry in turns] == [
        event_pb2.TURN_STATUS_COMPLETED,
        event_pb2.TURN_STATUS_COMPLETED,
    ]


if __name__ == "__main__":
    pytest_bazel.main()
