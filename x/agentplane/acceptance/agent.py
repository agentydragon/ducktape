"""One agent session, driven to the end of a turn.

An acceptance scenario asks a real harness to do something and then checks what the system recorded,
never what the agent said about it: the model's prose is not a contract, and a scenario that greps it
fails on a paraphrase. What the agent produced is returned all the same, because it is what makes a
failure readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from x.agentplane.acceptance.client import Client

# A turn on the cheap model with a few tool calls; a harness that stops making progress should fail
# the scenario rather than hold the suite until the Bazel timeout.
TURN_SECONDS = 300.0


@dataclass
class Turn:
    """What one turn produced: the harness's own tool outputs and its assistant text."""

    tool_outputs: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    status: str | None = None

    @property
    def transcript(self) -> str:
        """Everything the turn produced, for a failure message that says what the agent actually did."""
        return "\n".join([*(f"[tool] {output}" for output in self.tool_outputs), *(f"[text] {t}" for t in self.text)])


class Agent:
    """A session on one sandbox. `run` sends a prompt and returns when the turn completes."""

    def __init__(self, client: Client, *, sandbox: str, session_id: str) -> None:
        self._client = client
        self._sandbox = sandbox
        self._session_id = session_id
        self._sequence = 0

    @classmethod
    async def open(cls, client: Client, *, sandbox: str, provider: str, model: str) -> Agent:
        session_id = f"acceptance-{uuid4().hex[:8]}"
        # /state is the writable volume; the image's workspace is not writable by the runner's user.
        spec = {"provider": provider, "cwd": "/state/work", "model": model, "reasoningEffort": "low"}
        attached = await client.open_session(sandbox, session_id, spec)
        agent = cls(client, sandbox=sandbox, session_id=session_id)
        agent._sequence = int(attached.get("lastSequence", 0))
        return agent

    async def run(self, prompt: str) -> Turn:
        """Send `prompt` and collect the turn it starts. Nothing else drives this session, so reading
        the cursor before submitting cannot miss an event."""
        after = self._sequence
        await self._client.send_input(self._sandbox, self._session_id, f"input-{uuid4().hex[:8]}", prompt)
        turn = Turn()
        async for event in self._client.events(self._sandbox, self._session_id, after=after, read_seconds=TURN_SECONDS):
            if event.sequence is not None:
                self._sequence = event.sequence
            _collect(event.data, turn)
            if turn.status is not None:
                return turn
        raise AssertionError(f"the session's event stream ended before the turn completed: {turn.transcript}")


def _collect(data: dict[str, Any], turn: Turn) -> None:
    if (completed := data.get("itemCompleted")) is not None:
        if (tool := completed.get("tool")) is not None:
            turn.tool_outputs.append(str(tool.get("output", "")))
        elif (text := completed.get("text")) is not None:
            turn.text.append(str(text))
    if (turn_completed := data.get("turnCompleted")) is not None:
        turn.status = str(turn_completed.get("status", ""))
