"""The app submits each supported command through its authenticated Dispatcher client."""

from pathlib import Path
from typing import Any

import grpc
import pytest_bazel

from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, session_dispatcher_pb2


class Stub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, object]]] = []
        self.admission = event_log_pb2.EventEntry(
            cursor=1,
            origin=event_log_pb2.EventOrigin(source_id="runner", sequence=1),
            event=event_pb2.Event(
                command_admitted=event_pb2.CommandAdmitted(
                    command=command_pb2.Command(
                        command_id="interrupt", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn")
                    )
                )
            ),
        )

    async def _dispatch(self, name: str, request: Any, **kwargs: object) -> Any:
        self.calls.append((name, request, kwargs))
        dispatch = session_dispatcher_pb2.Dispatch(
            command_id=request.command_id,
            session_id=getattr(request, "session_id", "session-a"),
            sandbox=getattr(request, "sandbox", "sandbox-a"),
            state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
        )
        return {
            "SubmitInput": session_dispatcher_pb2.SubmitInputResponse(dispatch=dispatch),
            "ChangeModel": session_dispatcher_pb2.ChangeModelResponse(dispatch=dispatch),
            "ChangeReasoningEffort": session_dispatcher_pb2.ChangeReasoningEffortResponse(dispatch=dispatch),
            "StopRunnerSession": session_dispatcher_pb2.StopRunnerSessionResponse(dispatch=dispatch),
            "InterruptTurn": session_dispatcher_pb2.InterruptTurnResponse(
                state=session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED, admission=self.admission
            ),
            "GetDispatch": session_dispatcher_pb2.GetDispatchResponse(dispatch=dispatch),
        }[name]

    def __getattr__(self, name: str) -> Any:
        async def call(request: Any, **kwargs: object) -> Any:
            return await self._dispatch(name, request, **kwargs)

        return call


async def test_operations_keep_the_command_identity_and_use_the_dispatcher_audience(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("dispatcher-token\n")
    channel = grpc.aio.insecure_channel("localhost:1")
    client = SessionDispatcherClient(channel, token_file=token, request_timeout_s=7)
    stub = Stub()
    client.stub = stub
    commands = [
        command_pb2.Command(command_id="input", submit_input=command_pb2.SubmitInput(text="hello")),
        command_pb2.Command(command_id="model", change_model=command_pb2.ChangeModel(model="next")),
        command_pb2.Command(
            command_id="effort", change_reasoning_effort=command_pb2.ChangeReasoningEffort(effort="high")
        ),
        command_pb2.Command(command_id="stop", stop_runner_session=command_pb2.StopRunnerSession()),
        command_pb2.Command(command_id="interrupt", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn")),
    ]
    try:
        receipts = [
            await client.command(sandbox="sandbox-a", session_id="session-a", command=command) for command in commands
        ]
        assert [name for name, _, _ in stub.calls] == [
            "SubmitInput",
            "ChangeModel",
            "ChangeReasoningEffort",
            "StopRunnerSession",
            "InterruptTurn",
        ]
        assert [request.command_id for _, request, _ in stub.calls] == ["input", "model", "effort", "stop", "interrupt"]
        assert [receipt.state for receipt in receipts[:4]] == [
            session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
        ] * 4
        assert receipts[4].state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED
        assert receipts[4].admission == stub.admission
        for _, _, options in stub.calls:
            assert options["metadata"] == (("authorization", "Bearer dispatcher-token"),)
            assert options["timeout"] == 7

        fetched = await client.get_dispatch("input")
        assert fetched.command_id == "input"
        assert stub.calls[-1][0] == "GetDispatch"
        assert stub.calls[-1][1].command_id == "input"
    finally:
        await channel.close()


if __name__ == "__main__":
    pytest_bazel.main()
