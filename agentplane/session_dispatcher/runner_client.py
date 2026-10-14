"""Calls to a Sandbox's runner through the Runner Gateway's runner proxy, on its control Service."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from agentplane.grpc_calls import bearer, dial
from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2, runner_control_pb2_grpc, runner_pb2
from agentplane.runner_gateway.runner_proxy import SANDBOX_HEADER

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class RunnerClient:
    """A call's failure is the runner's own status, as `grpc.aio.AioRpcError`: the Dispatcher reads
    what to do from its code. Each call dials its own connection, because a replica that no longer
    holds the runner answers `UNAVAILABLE` and the next call must resolve the control Service afresh."""

    def __init__(self, *, control_target: str, namespace: str, token_file: Path, request_timeout_s: float) -> None:
        # A template of `{sandbox}` and `{namespace}`: `{sandbox}-control.{namespace}.svc:8080`.
        self.control_target = control_target
        self.namespace = namespace
        self.token_file = token_file
        self.request_timeout_s = request_timeout_s

    @asynccontextmanager
    async def _runner(
        self, sandbox: str
    ) -> AsyncIterator[tuple[runner_control_pb2_grpc.RunnerAsyncStub, tuple[tuple[str, str], ...]]]:
        metadata = (*await bearer(self.token_file), (SANDBOX_HEADER, sandbox))
        async with dial(self.control_target.format(sandbox=sandbox, namespace=self.namespace)) as channel:
            runner: runner_control_pb2_grpc.RunnerAsyncStub = runner_control_pb2_grpc.RunnerStub(channel)
            yield runner, metadata

    async def open_session(
        self, sandbox: str, session_id: str, spec: runner_pb2.SessionSpec, setup_script: str | None
    ) -> runner_pb2.Attached:
        """The runner's snapshot of the Session it created, or confirmed against the same spec."""
        request = runner_control_pb2.OpenSessionRequest(session_id=session_id, spec=spec, setup_script=setup_script)
        async with self._runner(sandbox) as (runner, metadata):
            return (await runner.OpenSession(request, metadata=metadata, timeout=self.request_timeout_s)).attached

    async def has_session(self, sandbox: str, session_id: str) -> bool:
        async with self._runner(sandbox) as (runner, metadata):
            listed = await runner.ListSessions(
                runner_pb2.ListSessionsRequest(), metadata=metadata, timeout=self.request_timeout_s
            )
        return any(summary.session_id == session_id for summary in listed.sessions)

    async def deliver(self, sandbox: str, session_id: str, command: command_pb2.Command) -> event_log_pb2.EventEntry:
        """The runner's admission of `command`. An input or a setting change first resumes a stopped
        harness, which leaves a running one alone; an interrupt or a stop never starts anything."""
        timeout = self.request_timeout_s
        async with self._runner(sandbox) as (runner, metadata):
            match command.WhichOneof("operation"):
                case "submit_input":
                    await runner.ResumeSession(
                        runner_control_pb2.ResumeSessionRequest(session_id=session_id),
                        metadata=metadata,
                        timeout=timeout,
                    )
                    return (
                        await runner.SubmitInput(
                            runner_control_pb2.SubmitInputRequest(
                                session_id=session_id, command_id=command.command_id, text=command.submit_input.text
                            ),
                            metadata=metadata,
                            timeout=timeout,
                        )
                    ).admission
                case "change_model":
                    await runner.ResumeSession(
                        runner_control_pb2.ResumeSessionRequest(session_id=session_id),
                        metadata=metadata,
                        timeout=timeout,
                    )
                    return (
                        await runner.ChangeModel(
                            runner_control_pb2.ChangeModelRequest(
                                session_id=session_id, command_id=command.command_id, model=command.change_model.model
                            ),
                            metadata=metadata,
                            timeout=timeout,
                        )
                    ).admission
                case "change_reasoning_effort":
                    await runner.ResumeSession(
                        runner_control_pb2.ResumeSessionRequest(session_id=session_id),
                        metadata=metadata,
                        timeout=timeout,
                    )
                    return (
                        await runner.ChangeReasoningEffort(
                            runner_control_pb2.ChangeReasoningEffortRequest(
                                session_id=session_id,
                                command_id=command.command_id,
                                effort=command.change_reasoning_effort.effort,
                            ),
                            metadata=metadata,
                            timeout=timeout,
                        )
                    ).admission
                case "interrupt_turn":
                    return (
                        await runner.InterruptTurn(
                            runner_control_pb2.InterruptTurnRequest(
                                session_id=session_id,
                                command_id=command.command_id,
                                turn_id=command.interrupt_turn.turn_id,
                            ),
                            metadata=metadata,
                            timeout=timeout,
                        )
                    ).admission
                case "stop_runner_session":
                    return (
                        await runner.StopRunnerSession(
                            runner_control_pb2.StopRunnerSessionRequest(
                                session_id=session_id, command_id=command.command_id
                            ),
                            metadata=metadata,
                            timeout=timeout,
                        )
                    ).admission
                case other:
                    raise ValueError(f"{other} is not a command operation")
