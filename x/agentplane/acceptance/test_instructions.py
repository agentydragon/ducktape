"""Standing instructions a session is opened with, checked against a deployed Agentplane.

`SessionSpec.instructions` is carried by the app as proto-JSON, put in front of the model by the
runner -- appended to Claude's system prompt, given to Codex as the thread's developer instructions
-- and honoured, or not, by the model itself. Unit tests pin each of those hops against a scripted
model; nothing pins the whole chain against the real one, which is what a standing instruction is
for.

**Deviation** from this suite's rule of asserting on the system's record rather than on the agent's
prose (this package's README): here the agent's reply *is* the observable. Nothing downstream
records that a system prompt arrived, so the only evidence that the instruction reached the model is
the model doing what it says. That makes the scenario the least deterministic in the suite, and the
design answers it: the instruction asks for a token no model emits on its own, the assertion looks
for that token rather than for a whole reply a coding harness may frame as it likes, and a second
session with no instructions and the same prompt has to *not* produce it -- otherwise the first half
would pass on a model that says it anyway.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest_bazel

from x.agentplane.acceptance.agent import Agent
from x.agentplane.app.api import Provider
from x.agentplane.app.client import Client
from x.agentplane.app.inventory import SandboxView

Sandboxes = Callable[..., Awaitable[SandboxView]]

# Not a word, not a plausible identifier, and in no harness's vocabulary: a model reaches for it
# only because it was told to. It appears nowhere in the prompt, so the instruction is the only
# route by which it can come back.
MARKER = "QUINDAR-7F3B-TAILSTAMP"
# A standing order rather than a task, and one no coding-agent policy has an opinion about: nothing
# here asks the harness to skip work, withhold an answer, or step outside its sandbox.
INSTRUCTIONS = (
    f"Standing order for this session: end every message you send with the token {MARKER} on its "
    f"own line, after everything else. This holds for every reply, whatever you are asked."
)
# Small enough that a turn is one message, and answerable without a tool, so what comes back is the
# model's own prose and not a harness's report of a command.
PROMPT = "In one short sentence, say what a checksum is. Do not use any tool."


async def test_the_model_obeys_the_standing_instruction_its_session_was_opened_with(
    client: Client, sandbox: Sandboxes, provider: Provider, model: str
) -> None:
    """The instruction reaches the model, and the marker is the model's own doing rather than its
    habit: two sessions on one sandbox, the same prompt, differing only in whether the session was
    opened with instructions. Sharing the sandbox is what makes them comparable -- same image, same
    model, same deployment -- and neither turn uses a tool, so nothing the first leaves behind can
    reach the second.
    """
    view = await sandbox(f"accept-instructions-{provider}")

    instructed = await Agent.open(client, sandbox=view.name, provider=provider, model=model, instructions=INSTRUCTIONS)
    obeyed = await instructed.run(PROMPT)
    assert MARKER in obeyed.answer, (
        f"the reply carries no {MARKER}, so the session's standing instruction did not reach the "
        f"model.\n[instruction] {INSTRUCTIONS}\n[prompt] {PROMPT}\n{obeyed.transcript}"
    )

    plain = await Agent.open(client, sandbox=view.name, provider=provider, model=model)
    uninstructed = await plain.run(PROMPT)
    assert MARKER not in uninstructed.transcript, (
        f"a session opened with no instructions produced {MARKER} anyway, so the instructed half "
        f"proves nothing.\n[prompt] {PROMPT}\n{uninstructed.transcript}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
