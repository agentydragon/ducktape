"""Transport-independent interpretation of one admitted harness turn."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from agentplane.proto import event_log_pb2, event_pb2

# A stalled harness must fail before the suite's overall Bazel deadline.
TURN_SECONDS = 300.0

Reported = TypeVar("Reported", bound=BaseModel)

# The last brace-delimited object in the answer, fenced or not: a model that explains itself before
# or after the JSON is still answering the question.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Turn:
    """What one turn produced: the harness's own tool outputs and its assistant text."""

    # `time.monotonic()` when the caller accepted the turn's input: the Dispatcher holds it, and no
    # native work need have started.
    accepted: float
    tool_results: list[event_pb2.ToolResult] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    # Every line the harness wrote during the turn, as the runner recorded it.
    native: list[str] = field(default_factory=list)
    status: event_pb2.TurnStatus | None = None
    input_confirmed: bool = False
    resumed: bool = False

    @property
    def tool_outputs(self) -> list[str]:
        return [result.output for result in self.tool_results]

    def shell_json(self, command: str) -> dict[str, Any]:
        """Decode the object printed by one successful shell tool, after any harness preamble."""
        assert len(self.tool_results) == 1, (
            f"{command!r} produced {len(self.tool_results)} tool results, expected one\n{self.transcript}"
        )
        (result,) = self.tool_results
        assert result.succeeded, f"{command!r} failed\n{self.transcript}"
        start = result.output.find("{")
        assert start >= 0, f"{command!r} returned no JSON object\n{self.transcript}"
        try:
            value, _ = json.JSONDecoder().raw_decode(result.output, idx=start)
        except json.JSONDecodeError as error:
            raise AssertionError(f"{command!r} returned invalid JSON: {error}\n{self.transcript}") from error
        assert isinstance(value, dict), f"{command!r} returned no JSON object\n{self.transcript}"
        return value

    @property
    def answer(self) -> str:
        """The turn's final assistant message: what the agent reports back."""
        if not self.text:
            raise AssertionError(f"the turn produced no assistant message:\n{self.transcript}")
        return self.text[-1]

    def report(self, shape: type[Reported]) -> Reported:
        """The JSON object the agent was asked to end with, as `shape`.

        A scenario states a goal and asks for a structured report, rather than dictating a command
        and grepping prose: the agent picks how, and the answer stays machine-checkable.
        """
        found = _OBJECT.search(self.answer)
        if found is None:
            raise AssertionError(f"the agent reported no JSON object:\n{self.transcript}")
        try:
            return shape.model_validate_json(found.group())
        except (ValueError, TypeError) as error:
            raise AssertionError(f"the agent's report is not a {shape.__name__}: {error}\n{self.answer}") from error

    @property
    def transcript(self) -> str:
        """Everything the turn produced, so a failure says what the agent actually did."""
        return "\n".join([*(f"[tool] {output}" for output in self.tool_outputs), *(f"[text] {t}" for t in self.text)])


class TurnCollector:
    """Refold corrected events and stop the caller cursor at the returned terminal.

    A page can include retained prior turns or entries beyond the requested turn. Exact command
    admission selects the requested work; terminal failures also advance the consumed cursor.
    """

    def __init__(self, *, after_cursor: int, awaiting_command_id: str | None, accepted: float) -> None:
        self.cursor = after_cursor
        self._awaiting_command_id = awaiting_command_id
        self._accepted = accepted

    def collect(self, entries: Sequence[event_log_pb2.EventEntry]) -> Turn | None:
        turn = Turn(accepted=self._accepted)
        command_admitted = self._awaiting_command_id is None
        for entry in entries:
            event = entry.event
            match event.WhichOneof("observation"):
                case "command_admitted" if event.command_admitted.command.command_id == self._awaiting_command_id:
                    command_admitted = True
                case "command_failed" if (
                    self._awaiting_command_id is None or event.command_failed.command_id == self._awaiting_command_id
                ):
                    failed = event.command_failed
                    self.cursor = entry.cursor
                    raise AssertionError(f"command {failed.command_id!r} failed: {failed.reason!r}")
                case _:
                    pass
            if event.HasField("harness_started"):
                # A resumed runner may start before the submitted command is admitted.
                turn.resumed = event.harness_started.resumed
            elif event.HasField("harness_user_message_confirmed") and (
                self._awaiting_command_id is None
                or self._awaiting_command_id in event.harness_user_message_confirmed.origin_command_ids
            ):
                turn.input_confirmed = True
            # A retained successful turn before this request's exact admission belongs to
            # prior work. Do not accept it, its outputs, or its terminal as this result.
            if not command_admitted:
                if event.HasField("turn_completed"):
                    # Discard lifecycle metadata that belonged to a prior completed turn.
                    turn.resumed = False
                    turn.input_confirmed = False
                continue
            self.cursor = entry.cursor
            match event.WhichOneof("observation"):
                case "harness_launch_failed":
                    raise AssertionError(f"harness launch failed: {event.harness_launch_failed.reason!r}")
                case "harness_started" | "harness_user_message_confirmed":
                    continue
                case "item_completed":
                    _completed(event.item_completed, turn)
                case "native" if event.native.direction == event_pb2.DIRECTION_FROM_HARNESS:
                    turn.native.append(event.native.line)
                case "turn_completed":
                    completed = event.turn_completed
                    turn.status = completed.status
                    if turn.status != event_pb2.TURN_STATUS_COMPLETED:
                        raise AssertionError(
                            f"turn {completed.turn_id!r} ended with "
                            f"{event_pb2.TurnStatus.Name(turn.status)}: {completed.error=}"
                        )
                    # A page may include data past the turn we are returning. Re-read it
                    # on the next call instead of advancing our caller cursor past it.
                    return turn
                case _:
                    continue
        return None


def _completed(item: event_pb2.ItemCompleted, turn: Turn) -> None:
    match item.WhichOneof("outcome"):
        case "tool":
            turn.tool_results.append(item.tool)
        case "text":
            turn.text.append(item.text)


# gazelle:include_dep @pypi//protobuf
