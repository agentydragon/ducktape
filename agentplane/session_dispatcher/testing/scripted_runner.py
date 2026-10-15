"""A runner on the Runner Gateway's runner proxy, answering from a script."""

from typing import override

import grpc

from agentplane.proto import event_log_pb2, runner_control_pb2, runner_control_pb2_grpc, runner_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio


class ScriptedRunner(runner_control_pb2_grpc.RunnerServicer):
    """Answers every call with an admission, or with the next scripted failure."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: list[tuple[grpc.StatusCode, str]] = []
        self.opened_sessions: dict[str, runner_control_pb2.OpenSessionRequest] = {}
        self.lost_open_replies: set[str] = set()
        self.resumed_sessions: list[str] = []

    async def _answer(self, call: str, command_id: str, context: grpc.aio.ServicerContext) -> event_log_pb2.EventEntry:
        self.calls.append((call, command_id))
        if self.failures:
            code, details = self.failures.pop(0)
            await context.abort(code, details)
        return event_log_pb2.EventEntry()

    @override
    async def OpenSession(
        self, request: runner_control_pb2.OpenSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.OpenSessionResponse:
        self.calls.append(("OpenSession", request.session_id))
        existing = self.opened_sessions.get(request.session_id)
        difference = None
        if existing is not None and existing.spec != request.spec:
            difference = "spec"
        elif existing is not None and request.HasField("setup_script"):
            existing_setup = existing.setup_script if existing.HasField("setup_script") else None
            if (request.setup_script or None) != (existing_setup or None):
                difference = "setup script"
        if difference is not None:
            await context.abort(
                grpc.StatusCode.ALREADY_EXISTS, f"session {request.session_id} exists with a different {difference}"
            )
        if existing is None:
            existing = runner_control_pb2.OpenSessionRequest()
            existing.CopyFrom(request)
            self.opened_sessions[request.session_id] = existing
        if request.session_id in self.lost_open_replies:
            self.lost_open_replies.remove(request.session_id)
            await context.abort(grpc.StatusCode.UNAVAILABLE, "opening reply lost after creation")
        return runner_control_pb2.OpenSessionResponse(attached=runner_pb2.Attached(session_id=request.session_id))

    @override
    async def ResumeSession(
        self, request: runner_control_pb2.ResumeSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.ResumeSessionResponse:
        self.resumed_sessions.append(request.session_id)
        return runner_control_pb2.ResumeSessionResponse()

    @override
    async def SubmitInput(
        self, request: runner_control_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.SubmitInputResponse:
        return runner_control_pb2.SubmitInputResponse(
            admission=await self._answer("SubmitInput", request.command_id, context)
        )

    @override
    async def InterruptTurn(
        self, request: runner_control_pb2.InterruptTurnRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.InterruptTurnResponse:
        return runner_control_pb2.InterruptTurnResponse(
            admission=await self._answer("InterruptTurn", request.command_id, context)
        )

    @override
    async def StopRunnerSession(
        self, request: runner_control_pb2.StopRunnerSessionRequest, context: grpc.aio.ServicerContext
    ) -> runner_control_pb2.StopRunnerSessionResponse:
        return runner_control_pb2.StopRunnerSessionResponse(
            admission=await self._answer("StopRunnerSession", request.command_id, context)
        )
