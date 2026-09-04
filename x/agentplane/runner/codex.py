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

import itertools
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from x.agentplane.native.codex import driver, scenarios, wire
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.config import CodexLaunch

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

if TYPE_CHECKING:
    from x.agentplane.runner.session import Frame, Session

_TURN_STATUSES: dict[wire.TurnStatus | str, pb.TurnStatus.ValueType] = {
    wire.TurnStatus.COMPLETED: pb.TURN_STATUS_COMPLETED,
    wire.TurnStatus.INTERRUPTED: pb.TURN_STATUS_INTERRUPTED,
    wire.TurnStatus.FAILED: pb.TURN_STATUS_FAILED,
}
# Fields of an unmodeled tool item that describe its outcome rather than its arguments.
_OUTCOME_FIELDS = frozenset({"status", "aggregatedOutput", "exitCode", "durationMs", "processId"})


class CodexAdapter(HarnessAdapter):
    def __init__(self, session: Session, launch: CodexLaunch) -> None:
        self.session = session
        self.launch = launch
        self._thread_id = session.record.native_session_id or ""
        self._request_ids = (f"agentplane-{n}" for n in itertools.count(1))
        self._items: set[str] = set()

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
        await self._request(driver.initialize(next(self._request_ids)))
        await self.session.write_native(driver.initialized())
        record = self.session.record
        if self._thread_id:
            frame: wire.ThreadStartRequest | wire.ThreadResumeRequest = driver.thread_resume(
                next(self._request_ids), thread_id=self._thread_id
            )
        else:
            # A thread takes the session's standing instructions once, when it is created; the
            # resume branch above reaches an app-server that already has them.
            frame = driver.thread_start(
                next(self._request_ids),
                cwd=record.cwd,
                model=record.model,
                effort=record.reasoning_effort,
                persist=True,
                instructions=record.instructions,
            )
        response, _ = await self._request(frame)
        if response.error is not None:
            raise RuntimeError(f"Codex refused {frame.method}: {response.error.message}")
        if response.result is None:
            raise RuntimeError(f"Codex {frame.method} returned no result")
        self._thread_id = wire.ThreadResult.model_validate(response.result).thread.id
        return self._thread_id

    async def submit(self, input_id: str, text: str) -> None:
        self.session.emit(pb.InputSubmitted(input_id=input_id, text=text), sources=[])
        response, sequence = await self._request(
            driver.turn_start(next(self._request_ids), thread_id=self._thread_id, text=text)
        )
        if response.error is not None or response.result is None:
            reason = response.error.message if response.error is not None else "turn/start returned no result"
            self.session.emit(pb.InputRejected(input_id=input_id, reason=reason), sources=[sequence])
            return
        turn_id = wire.TurnResult.model_validate(response.result).turn.id
        if turn_id != self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=turn_id), sources=[sequence])
        self.session.emit(pb.InputAccepted(input_id=input_id, turn_id=turn_id), sources=[sequence])

    async def interrupt(self) -> None:
        await self._request(
            driver.interrupt(next(self._request_ids), thread_id=self._thread_id, turn_id=self.session.active_turn_id)
        )

    async def on_frame(self, frame: Frame) -> None:
        match wire.parse_frame(frame):
            case wire.ServerRequest(id=request_id, method=method):
                # Approvals, user-input requests, and elicitations have no answer path here; a
                # refusal keeps the turn moving instead of blocking it forever.
                await self.session.write_native(
                    wire.ErrorResponse(
                        id=request_id,
                        error=wire.RpcError(code=-32601, message=f"the agentplane runner does not answer {method}"),
                    )
                )
            case wire.TurnStarted(params=params):
                if params.turn.id != self.session.active_turn_id:
                    self.session.emit(pb.TurnStarted(turn_id=params.turn.id))
            case wire.TurnCompleted(params=params):
                turn = params.turn
                status = _TURN_STATUSES.get(turn.status)
                if status is None:
                    # A terminal status these models do not know cannot be reported as success.
                    status, error = pb.TURN_STATUS_FAILED, f"the turn ended with an unrecognized status {turn.status!r}"
                else:
                    error = turn.error.message if turn.error is not None else ""
                self.session.emit(pb.TurnCompleted(turn_id=turn.id, status=status, error=error))
            case wire.ItemStarted(params=params):
                self._item_started(params.item)
            case wire.ItemCompleted(params=params):
                self._item_completed(params.item)
            case wire.AgentMessageDelta(params=params) | wire.ReasoningSummaryTextDelta(params=params):
                self.session.emit(pb.TextDelta(item_id=params.item_id, text=params.delta))
            case wire.CommandExecutionOutputDelta(params=params):
                self.session.emit(pb.ToolOutputDelta(item_id=params.item_id, text=params.delta))

    def _item_started(self, item: wire.Item) -> None:
        if isinstance(item, wire.UserMessageItem) or item.id in self._items:
            return
        self._items.add(item.id)
        match item:
            case wire.AgentMessageItem():
                self.session.emit(pb.ItemStarted(item_id=item.id, kind=pb.ITEM_KIND_ASSISTANT_TEXT))
            case wire.ReasoningItem():
                self.session.emit(pb.ItemStarted(item_id=item.id, kind=pb.ITEM_KIND_REASONING))
            case wire.CommandExecutionItem():
                self.session.emit(pb.ItemStarted(item_id=item.id, kind=pb.ITEM_KIND_TOOL_CALL, tool_name=item.type))
                arguments: dict[str, object] = {"command": item.command, "cwd": item.cwd}
                self.session.emit(pb.ToolArguments(item_id=item.id, arguments_json=json.dumps(arguments)))
            case wire.UnknownItem():
                self.session.emit(pb.ItemStarted(item_id=item.id, kind=pb.ITEM_KIND_TOOL_CALL, tool_name=item.type))
                arguments = {key: value for key, value in _extras(item).items() if key not in _OUTCOME_FIELDS}
                self.session.emit(pb.ToolArguments(item_id=item.id, arguments_json=json.dumps(arguments)))

    def _item_completed(self, item: wire.Item) -> None:
        if isinstance(item, wire.UserMessageItem):
            return
        self._item_started(item)
        match item:
            case wire.AgentMessageItem(text=text):
                self.session.emit(pb.ItemCompleted(item_id=item.id, text=text))
            case wire.ReasoningItem(summary=summary):
                self.session.emit(pb.ItemCompleted(item_id=item.id, text="\n".join(summary)))
            case wire.CommandExecutionItem():
                self.session.emit(
                    pb.ItemCompleted(
                        item_id=item.id,
                        tool=pb.ToolResult(
                            output=item.aggregated_output or "",
                            succeeded=item.status is wire.CommandExecutionStatus.COMPLETED,
                        ),
                    )
                )
            case wire.UnknownItem():
                outcome = {key: value for key, value in _extras(item).items() if key in _OUTCOME_FIELDS}
                self.session.emit(
                    pb.ItemCompleted(
                        item_id=item.id,
                        tool=pb.ToolResult(output=json.dumps(outcome), succeeded=outcome.get("status") == "completed"),
                    )
                )

    async def _request(self, frame: wire.Request) -> tuple[wire.Response, int]:
        """Send a request and return its response with the Native sequence it arrived as."""
        native = await self.session.request(frame, matches=lambda candidate: candidate.get("id") == frame.id)
        return wire.Response.model_validate(native.frame), native.sequence


def _extras(item: wire.UnknownItem) -> dict[str, object]:
    return dict(item.model_extra or {})
