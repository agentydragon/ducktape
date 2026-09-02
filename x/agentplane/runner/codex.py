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
from typing import TYPE_CHECKING, Any

from x.agentplane.native.codex import driver, scenarios
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.adapter import HarnessAdapter
from x.agentplane.runner.config import CodexLaunch

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

if TYPE_CHECKING:
    from x.agentplane.runner.session import Frame, Session

_TURN_STATUSES = {
    "completed": pb.TURN_STATUS_COMPLETED,
    "interrupted": pb.TURN_STATUS_INTERRUPTED,
    "failed": pb.TURN_STATUS_FAILED,
}
# Fields of a completed tool item that describe its outcome rather than its arguments.
_OUTCOME_FIELDS = frozenset({"id", "type", "status", "aggregatedOutput", "exitCode", "durationMs", "processId"})


class CodexAdapter(HarnessAdapter):
    def __init__(self, session: Session, launch: CodexLaunch) -> None:
        self.session = session
        self.launch = launch
        self._thread_id = session.record.native_session_id or ""
        self._request_ids = (f"agentplane-{n}" for n in itertools.count(1))
        self._items: dict[str, int] = {}

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
            frame = driver.thread_resume(next(self._request_ids), thread_id=self._thread_id)
        else:
            frame = driver.thread_start(
                next(self._request_ids),
                cwd=record.cwd,
                model=record.model,
                effort=record.reasoning_effort,
                persist=True,
            )
        response = await self._request(frame)
        if "error" in response.frame:
            raise RuntimeError(f"Codex refused {frame['method']}: {response.frame['error']}")
        self._thread_id = str(_dict(_dict(response.frame.get("result")).get("thread")).get("id", ""))
        if not self._thread_id:
            raise RuntimeError(f"Codex {frame['method']} returned no thread id")
        return self._thread_id

    async def submit(self, input_id: str, text: str) -> None:
        self.session.emit(pb.InputSubmitted(input_id=input_id), sources=[])
        response = await self._request(driver.turn_start(next(self._request_ids), thread_id=self._thread_id, text=text))
        if "error" in response.frame:
            reason = str(_dict(response.frame.get("error")).get("message", response.frame["error"]))
            self.session.emit(pb.InputRejected(input_id=input_id, reason=reason), sources=[response.sequence])
            return
        turn_id = str(_dict(_dict(response.frame.get("result")).get("turn")).get("id", ""))
        if turn_id != self.session.active_turn_id:
            self.session.emit(pb.TurnStarted(turn_id=turn_id), sources=[response.sequence])
        self.session.emit(pb.InputAccepted(input_id=input_id, turn_id=turn_id), sources=[response.sequence])

    async def interrupt(self) -> None:
        await self._request(
            driver.interrupt(next(self._request_ids), thread_id=self._thread_id, turn_id=self.session.active_turn_id)
        )

    async def on_frame(self, frame: Frame) -> None:
        method = frame.get("method")
        if not isinstance(method, str):
            return
        if "id" in frame:
            # Approvals, user-input requests, and elicitations have no answer path here; a refusal
            # keeps the turn moving instead of blocking it forever.
            await self.session.write_native(
                {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": f"the agentplane runner does not answer {method}"},
                }
            )
            return
        params = _dict(frame.get("params"))
        match method:
            case "turn/started":
                turn_id = str(_dict(params.get("turn")).get("id", ""))
                if turn_id and turn_id != self.session.active_turn_id:
                    self.session.emit(pb.TurnStarted(turn_id=turn_id))
            case "turn/completed":
                turn = _dict(params.get("turn"))
                status = str(turn.get("status", ""))
                if status not in _TURN_STATUSES:
                    raise ValueError(f"unknown Codex turn {status=}")
                error = str(_dict(turn.get("error")).get("message", ""))
                self.session.emit(
                    pb.TurnCompleted(turn_id=str(turn.get("id", "")), status=_TURN_STATUSES[status], error=error)
                )
            case "item/started":
                self._item_started(_dict(params.get("item")))
            case "item/completed":
                self._item_completed(_dict(params.get("item")))
            case "item/agentMessage/delta" | "item/reasoning/summaryTextDelta":
                self.session.emit(
                    pb.TextDelta(item_id=str(params.get("itemId", "")), text=str(params.get("delta", "")))
                )
            case "item/commandExecution/outputDelta":
                self.session.emit(
                    pb.ToolOutputDelta(item_id=str(params.get("itemId", "")), text=str(params.get("delta", "")))
                )

    def _item_started(self, item: Frame) -> None:
        item_id, item_type = str(item.get("id", "")), str(item.get("type", ""))
        if item_type == "userMessage" or item_id in self._items:
            return
        match item_type:
            case "agentMessage":
                kind, tool_name = pb.ITEM_KIND_ASSISTANT_TEXT, ""
            case "reasoning":
                kind, tool_name = pb.ITEM_KIND_REASONING, ""
            case _:
                kind, tool_name = pb.ITEM_KIND_TOOL_CALL, item_type
        self._items[item_id] = kind
        self.session.emit(pb.ItemStarted(item_id=item_id, kind=kind, tool_name=tool_name))
        if kind == pb.ITEM_KIND_TOOL_CALL:
            arguments = {key: value for key, value in item.items() if key not in _OUTCOME_FIELDS}
            self.session.emit(pb.ToolArguments(item_id=item_id, arguments_json=json.dumps(arguments)))

    def _item_completed(self, item: Frame) -> None:
        item_id, item_type = str(item.get("id", "")), str(item.get("type", ""))
        if item_type == "userMessage":
            return
        self._item_started(item)
        match item_type:
            case "agentMessage":
                self.session.emit(pb.ItemCompleted(item_id=item_id, text=str(item.get("text", ""))))
            case "reasoning":
                summary = item.get("summary")
                text = "\n".join(str(part) for part in summary) if isinstance(summary, list) else ""
                self.session.emit(pb.ItemCompleted(item_id=item_id, text=text))
            case "commandExecution":
                output = item.get("aggregatedOutput")
                self.session.emit(
                    pb.ItemCompleted(
                        item_id=item_id,
                        tool=pb.ToolResult(
                            output=output if isinstance(output, str) else "",
                            succeeded=item.get("status") == "completed",
                        ),
                    )
                )
            case _:
                outcome = {
                    key: value
                    for key, value in item.items()
                    if key in _OUTCOME_FIELDS or key not in self._arguments(item)
                }
                self.session.emit(
                    pb.ItemCompleted(
                        item_id=item_id,
                        tool=pb.ToolResult(output=json.dumps(outcome), succeeded=item.get("status") == "completed"),
                    )
                )

    @staticmethod
    def _arguments(item: Frame) -> dict[str, Any]:
        return {key: value for key, value in item.items() if key not in _OUTCOME_FIELDS}

    async def _request(self, frame: Frame) -> Any:
        return await self.session.request(frame, matches=lambda candidate: candidate.get("id") == frame["id"])


def _dict(value: object) -> Frame:
    return value if isinstance(value, dict) else {}
