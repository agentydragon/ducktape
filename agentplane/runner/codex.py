"""Codex app-server over JSON-RPC: launch, input, interrupt, and frame translation.

Observed with Codex app-server 0.152.0:

- `turn/start` answers synchronously with the turn, and a second `turn/start` during a turn answers
  with the same turn id: the input joined it;
- `turn/started` and `turn/completed` notifications bracket a turn; items arrive as `item/started`,
  per-kind deltas, and `item/completed`, each naming the turn;
- `turn/interrupt` answers with an empty result, and the turn completes as `interrupted`;
- a server request (a frame with both `method` and `id`) blocks the turn until answered.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agentplane.native.codex import facade, scenarios, wire
from agentplane.protocol import event_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import CodexLaunch

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

if TYPE_CHECKING:
    from agentplane.runner.session import Frame, Session

_TURN_STATUSES: dict[wire.TurnStatus | str, event_pb2.TurnStatus] = {
    wire.TurnStatus.COMPLETED: event_pb2.TURN_STATUS_COMPLETED,
    wire.TurnStatus.INTERRUPTED: event_pb2.TURN_STATUS_INTERRUPTED,
    wire.TurnStatus.FAILED: event_pb2.TURN_STATUS_FAILED,
}
# Fields of an unmodeled tool item that describe its outcome rather than its arguments.
_OUTCOME_FIELDS = frozenset({"status", "aggregatedOutput", "exitCode", "durationMs", "processId"})


@dataclass(frozen=True)
class _TurnStart:
    """The input a `turn/start` carries, and the model command whose effect its answer proves."""

    command_id: str
    text: str
    model_change: tuple[str, str] | None


class CodexAdapter(HarnessAdapter):
    def __init__(self, session: Session, launch: CodexLaunch) -> None:
        self.session = session
        self.launch = launch
        self._thread_id = session.record.native_session_id or ""
        self.harness = facade.CodexHarness(session, request_prefix="agentplane")
        self._adapter_id = str(uuid4())
        # Codex chooses the model in turn/start. A received ChangeModel remains here until a
        # subsequent user command actually starts a turn using it.
        self._pending_model_changes: list[tuple[str, str]] = []
        # Sent `turn/start` requests by id, until `on_frame` translates Codex's answer.
        self._turn_starts: dict[wire.RequestId, _TurnStart] = {}

    def command(self) -> list[str]:
        return scenarios.command(str(self.launch.binary), endpoint=self.launch.base_url)

    def environment(self) -> Mapping[str, str]:
        # Codex refuses to start without an existing CODEX_HOME.
        codex_home = self.session.directory / "codex"
        codex_home.mkdir(exist_ok=True)
        return {
            **self.session.config.environment,
            **scenarios.environment(
                endpoint=self.launch.base_url, token=self.launch.api_key, codex_home=str(codex_home)
            ),
        }

    async def handshake(self) -> str:
        await self.harness.initialize()
        record = self.session.record
        if self._thread_id:
            response = await self.harness.resume_thread(thread_id=self._thread_id)
            method = "thread/resume"
        else:
            # A thread takes the session's standing instructions once, when it is created. The
            # resume branch above cannot restate them: `ensure_running` only handshakes a process
            # it just spawned, so the resumed thread replays them out of its rollout, and a
            # `developerInstructions` override on the resume would be accepted and ignored.
            response = await self.harness.start_thread(
                cwd=record.cwd,
                model=record.model,
                effort=record.reasoning_effort,
                persist=True,
                instructions=record.instructions,
            )
            method = "thread/start"
        if response.response.error is not None:
            raise RuntimeError(f"Codex refused {method}: {response.response.error.message}")
        if response.response.result is None:
            raise RuntimeError(f"Codex {method} returned no result")
        self._thread_id = wire.ThreadResult.model_validate(response.response.result).thread.id
        return self._thread_id

    async def submit(self, command_id: str, text: str) -> None:
        model_change = await self._take_pending_model_change()
        model = model_change[1] if model_change is not None else self.session.record.model
        request = self.harness.turn_start_request(thread_id=self._thread_id, text=text, model=model)
        self._turn_starts[request.id] = _TurnStart(command_id, text, model_change)
        try:
            # `on_frame` records what the answer proves. Waiting for it keeps normal commands one
            # turn/start at a time.
            await self.harness.request(request)
        finally:
            self._turn_starts.pop(request.id, None)

    async def interrupt(self, turn_id: str) -> None:
        await self.harness.interrupt(thread_id=self._thread_id, turn_id=turn_id)

    async def change_model(self, command_id: str, model: str) -> None:
        self._pending_model_changes.append((command_id, model))

    async def _take_pending_model_change(self) -> tuple[str, str] | None:
        """Choose the newest requested model for the next native turn.

        Earlier pending requests never changed a native selection, so they are terminal no-ops
        rather than fabricated model effects. The caller records the final command only after the
        matching native ``turn/start`` response confirms the selected model's actual use.
        """
        if not self._pending_model_changes:
            return None
        *superseded, selected = self._pending_model_changes
        self._pending_model_changes = []
        for command_id, _ in superseded:
            await self.session._noop(
                command_id, "superseded by a later model command before any Codex turn selected it", sources=[]
            )
        return selected

    async def on_frame(self, frame: Frame, source_sequence: int) -> None:
        sources = [source_sequence]
        match wire.parse_frame(frame):
            case wire.Response() as response if response.id in self._turn_starts:
                await self._turn_start_answered(self._turn_starts.pop(response.id), response, sources)
            case wire.ServerRequest(id=request_id, method=method):
                # Approvals, user-input requests, and elicitations have no answer path here; a
                # refusal keeps the turn moving instead of blocking it forever.
                await self.harness.send(
                    wire.ErrorResponse(
                        id=request_id,
                        error=wire.RpcError(code=-32601, message=f"the agentplane runner does not answer {method}"),
                    )
                )
            case wire.TurnStarted(params=params):
                if params.turn.id != self.session.active_turn_id:
                    await self.session.emit(
                        event_pb2.TurnStarted(turn_id=params.turn.id, model=self.session.record.model), sources=sources
                    )
            case wire.TurnCompleted(params=params):
                turn = params.turn
                status = _TURN_STATUSES.get(turn.status)
                if status is None:
                    # A terminal status these models do not know cannot be reported as success.
                    status, error = (
                        event_pb2.TURN_STATUS_FAILED,
                        f"the turn ended with an unrecognized status {turn.status!r}",
                    )
                else:
                    error = turn.error.message if turn.error is not None else ""
                await self.session.turn_completed(turn.id, status, error, sources=sources)
            case wire.ItemStarted(params=params):
                await self._item_started(params.item, sources)
            case wire.ItemCompleted(params=params):
                await self._item_completed(params.item, sources)
            case wire.AgentMessageDelta(params=params) | wire.ReasoningSummaryTextDelta(params=params):
                await self.session.emit(event_pb2.TextDelta(item_id=params.item_id, text=params.delta), sources=sources)
            case wire.CommandExecutionOutputDelta(params=params):
                await self.session.emit(
                    event_pb2.ToolOutputDelta(item_id=params.item_id, text=params.delta), sources=sources
                )

    async def _turn_start_answered(self, start: _TurnStart, response: wire.Response, sources: list[int]) -> None:
        if response.error is not None or response.result is None:
            reason = response.error.message if response.error is not None else "turn/start returned no result"
            if start.model_change is not None:
                await self.session._fail(
                    start.model_change[0], f"Codex did not select the requested model: {reason}", sources=sources
                )
            await self.session._fail(start.command_id, reason, sources=sources)
            return
        if start.model_change is not None:
            # The answer proves Codex accepted the turn that selected this model. This, rather than
            # command receipt or a guessed future boundary, is its causal effect.
            await self.session.model_changed(*start.model_change, sources=sources)
        turn_id = wire.TurnResult.model_validate(response.result).turn.id
        if turn_id != self.session.active_turn_id:
            await self.session.emit(
                event_pb2.TurnStarted(turn_id=turn_id, model=self.session.record.model), sources=sources
            )
        await self.session.confirm_user_message(
            harness_message_id=turn_id,
            text=start.text,
            origin_command_ids=[start.command_id],
            turn_id=turn_id,
            sources=sources,
        )

    async def _item_started(self, item: wire.Item, sources: list[int]) -> None:
        if isinstance(item, wire.UserMessageItem):
            return
        if not await self.session.journal.remember_adapter_item(self._adapter_id, item.id):
            return
        match item:
            case wire.AgentMessageItem():
                await self.session.emit(
                    event_pb2.ItemStarted(item_id=item.id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT), sources=sources
                )
            case wire.ReasoningItem():
                await self.session.emit(
                    event_pb2.ItemStarted(item_id=item.id, kind=event_pb2.ITEM_KIND_REASONING), sources=sources
                )
            case wire.CommandExecutionItem():
                await self.session.emit(
                    event_pb2.ItemStarted(item_id=item.id, kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name=item.type),
                    sources=sources,
                )
                arguments: dict[str, object] = {"command": item.command, "cwd": item.cwd}
                await self.session.emit(
                    event_pb2.ToolArguments(item_id=item.id, arguments_json=json.dumps(arguments)), sources=sources
                )
            case wire.UnknownItem():
                await self.session.emit(
                    event_pb2.ItemStarted(item_id=item.id, kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name=item.type),
                    sources=sources,
                )
                arguments = {key: value for key, value in _extras(item).items() if key not in _OUTCOME_FIELDS}
                await self.session.emit(
                    event_pb2.ToolArguments(item_id=item.id, arguments_json=json.dumps(arguments)), sources=sources
                )

    async def _item_completed(self, item: wire.Item, sources: list[int]) -> None:
        if isinstance(item, wire.UserMessageItem):
            return
        await self._item_started(item, sources)
        match item:
            case wire.AgentMessageItem(text=text):
                await self.session.emit(event_pb2.ItemCompleted(item_id=item.id, text=text), sources=sources)
            case wire.ReasoningItem(summary=summary):
                await self.session.emit(
                    event_pb2.ItemCompleted(item_id=item.id, text="\n".join(summary)), sources=sources
                )
            case wire.CommandExecutionItem():
                await self.session.emit(
                    event_pb2.ItemCompleted(
                        item_id=item.id,
                        tool=event_pb2.ToolResult(
                            output=item.aggregated_output or "",
                            succeeded=item.status is wire.CommandExecutionStatus.COMPLETED,
                        ),
                    ),
                    sources=sources,
                )
            case wire.UnknownItem():
                outcome = {key: value for key, value in _extras(item).items() if key in _OUTCOME_FIELDS}
                await self.session.emit(
                    event_pb2.ItemCompleted(
                        item_id=item.id,
                        tool=event_pb2.ToolResult(
                            output=json.dumps(outcome), succeeded=outcome.get("status") == "completed"
                        ),
                    ),
                    sources=sources,
                )


def _extras(item: wire.UnknownItem) -> dict[str, object]:
    return dict(item.model_extra or {})
