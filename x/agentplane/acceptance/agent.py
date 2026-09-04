"""One agent session, driven to the end of a turn.

A scenario asks a real harness to do something and then checks what the system recorded, never what
the agent said about it: the model's prose is not a contract, and a scenario that greps it fails on
a paraphrase. What the agent produced is collected all the same, because it is what makes a failure
readable.

Which harness runs is the caller's, not this module's: the runner protocol is the same for both, so
a scenario parametrised over `Provider` gets Claude and Codex from one body.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel
from tenacity import AsyncRetrying, retry_if_exception, stop_after_delay, wait_fixed

from x.agentplane.app.api import Provider
from x.agentplane.app.client import PROTO_PROVIDERS, Client
from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# A turn on the cheap model with a few tool calls. A harness that stops making progress should fail
# its scenario rather than hold the suite until Bazel's clock runs out.
TURN_SECONDS = 300.0
# The runner's writable volume; the image's workspace is not writable by the runner's user.
WORKING_DIRECTORY = "/state/work"
# How long a Running sandbox may still be short of a listening runner. Generous because the gap is
# a container start, not a fixed cost, and a scenario that waits a few seconds here is cheaper than
# one that fails a whole run.
RUNNER_ANSWERS_SECONDS = 120.0


def _runner_not_answering(error: BaseException) -> bool:
    """The app's `503`, and only that: it means the runner has not answered yet. Its `409`s -- an
    unreachable sandbox, a refused Open -- are verdicts, not delays, and must fail immediately."""
    return isinstance(error, httpx.HTTPStatusError) and error.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


Reported = TypeVar("Reported", bound=BaseModel)

# The last brace-delimited object in the answer, fenced or not: a model that explains itself before
# or after the JSON is still answering the question.
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Turn:
    """What one turn produced: the harness's own tool outputs and its assistant text."""

    tool_outputs: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    status: pb.TurnStatus.ValueType | None = None

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
            return shape.model_validate(json.loads(found.group()))
        except (ValueError, TypeError) as error:
            raise AssertionError(f"the agent's report is not a {shape.__name__}: {error}\n{self.answer}") from error

    @property
    def transcript(self) -> str:
        """Everything the turn produced, so a failure says what the agent actually did."""
        return "\n".join([*(f"[tool] {output}" for output in self.tool_outputs), *(f"[text] {t}" for t in self.text)])


class Agent:
    """A session on one sandbox. `run` sends a prompt and returns when the turn completes."""

    def __init__(self, client: Client, *, sandbox: str, session_id: str, sequence: int) -> None:
        self._client = client
        self._sandbox = sandbox
        self._session_id = session_id
        self._sequence = sequence

    @classmethod
    async def open(
        cls, client: Client, *, sandbox: str, provider: Provider, model: str, instructions: str = ""
    ) -> Agent:
        """Open a session, waiting out a runner that is up but not yet listening.

        A sandbox reports Running once its Pod has an address, and an address is not a listening
        runner: the app answers `503` for exactly that gap. Nothing read-only reaches the runner, so
        the open is the only thing that can be waited on -- which is still the condition and not a
        delay, and any other status fails on the first attempt as it should.

        `instructions` are the session's standing orders, in front of the model on every turn; empty
        is the proto's own default and opens the session the runner would open without the field.
        """
        spec = pb.SessionSpec(
            provider=PROTO_PROVIDERS[provider],
            cwd=WORKING_DIRECTORY,
            model=model,
            reasoning_effort="low",
            instructions=instructions,
        )
        # One id across attempts: a 503 is raised before the runner is reached, so no attempt can
        # have left a session behind under a name the next one would not reuse.
        session_id = f"acceptance-{uuid4().hex[:8]}"
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(RUNNER_ANSWERS_SECONDS),
            wait=wait_fixed(2),
            retry=retry_if_exception(_runner_not_answering),
            reraise=True,
        ):
            with attempt:
                attachment = await client.open_session(sandbox, session_id, spec)
                return cls(
                    client,
                    sandbox=sandbox,
                    session_id=attachment.attached.session_id,
                    sequence=attachment.last_sequence,
                )
        raise AssertionError("unreachable: reraise=True either returns an agent or raises")

    async def run(self, prompt: str) -> Turn:
        """Send `prompt` and collect the turn it starts. Nothing else drives this session, so reading
        the cursor before submitting cannot miss an event."""
        after = self._sequence
        await self._client.send_input(
            self._sandbox, self._session_id, pb.Input(input_id=f"input-{uuid4().hex[:8]}", text=prompt)
        )
        turn = Turn()
        async for event in self._client.events(self._sandbox, self._session_id, after=after, read_seconds=TURN_SECONDS):
            self._sequence = event.sequence
            match event.WhichOneof("observation"):
                case "item_completed":
                    _completed(event.item_completed, turn)
                case "turn_completed":
                    turn.status = event.turn_completed.status
                    return turn
                case _:
                    continue
        raise AssertionError(f"the session's stream ended before the turn completed:\n{turn.transcript}")


def _completed(item: pb.ItemCompleted, turn: Turn) -> None:
    match item.WhichOneof("outcome"):
        case "tool":
            turn.tool_outputs.append(item.tool.output)
        case "text":
            turn.text.append(item.text)
