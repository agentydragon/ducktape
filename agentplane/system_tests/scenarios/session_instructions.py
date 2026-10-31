"""Standing instructions a session is opened with, checked against a deployed Agentplane.

`SessionSpec.instructions` is carried by the app as proto-JSON, put in front of the model by the
runner -- appended to Claude's system prompt, given to Codex as the thread's developer instructions
-- and honoured, or not, by the model itself. The scripted scenario checks that the deployed chain
delivers them to the model; the real one checks that a model obeys them.

The real-model scenario is a **deviation** from this suite's rule of asserting on the system's record
rather than on the agent's prose (this package's README): whether a model does what the instruction
says is the behavior under test, so the reply *is* the observable. That makes it the least
deterministic scenario in the suite, and the design answers it: the instruction asks for a token no model emits on its own, the assertion looks
for that token rather than for a whole reply a coding harness may frame as it likes, and a second
session with no instructions and the same prompt has to *not* produce it -- otherwise the first half
would pass on a model that says it anyway.
"""

from __future__ import annotations

import asyncio

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.tracing import tracer
from agentplane.testing.scripted_model import Text

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


async def the_model_obeys_the_standing_instruction_its_session_was_opened_with(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, model: str
) -> None:
    """The instruction reaches the model, and the marker is the model's own doing rather than its
    habit: two sessions on one sandbox, the same prompt, differing only in whether the session was
    opened with instructions. Sharing the sandbox is what makes them comparable -- same image, same
    model, same deployment -- and neither turn uses a tool, so nothing the first leaves behind can
    reach the second.
    """
    view = await sandbox(f"systest-instructions-{harness}")

    with tracer.start_as_current_span(
        "system_test.session_instructions.instructed_turn", record_exception=False, set_status_on_exception=False
    ):
        instructed = await Agent.open(
            client, sandbox=view.name, harness=harness, model=model, instructions=INSTRUCTIONS
        )
        obeyed = await instructed.run(PROMPT)
    assert MARKER in obeyed.answer, (
        f"the reply carries no {MARKER}, so the session's standing instruction did not reach the "
        f"model.\n[instruction] {INSTRUCTIONS}\n[prompt] {PROMPT}\n{obeyed.transcript}"
    )

    with tracer.start_as_current_span(
        "system_test.session_instructions.plain_turn", record_exception=False, set_status_on_exception=False
    ):
        plain = await Agent.open(client, sandbox=view.name, harness=harness, model=model)
        uninstructed = await plain.run(PROMPT)
    assert MARKER not in uninstructed.transcript, (
        f"a session opened with no instructions produced {MARKER} anyway, so the instructed half "
        f"proves nothing.\n[prompt] {PROMPT}\n{uninstructed.transcript}"
    )


async def the_standing_instruction_reaches_the_model_and_only_in_its_own_session(
    client: Client, sandbox: Sandboxes, harness: runner_pb2.Harness, offered_model: str, scripted: ScriptedUpstream
) -> None:
    """Two sessions on one sandbox, as in the real-model scenario: what each sends the model shows
    whether the app, the Dispatcher and the runner carried the instruction, and to that session only."""
    view = await sandbox(f"systest-instructions-{harness}")
    assert view.pod is not None
    model = scripted.model(view.pod.name, harness)

    instructed = await Agent.open(
        client, sandbox=view.name, harness=harness, model=offered_model, instructions=INSTRUCTIONS
    )
    with tracer.start_as_current_span(
        "system_test.session_instructions.instructed_exchange", record_exception=False, set_status_on_exception=False
    ):
        async with asyncio.TaskGroup() as turns:
            obeyed = turns.create_task(instructed.run(PROMPT))
            request = await model.request()
            assert INSTRUCTIONS in request.system_text, request.system_text
            await model.reply(request, Text(f"A checksum detects corrupted data.\n{MARKER}"))
    # The scripted reply is the thread's answer: the model's side of the turn came back through ingress.
    assert MARKER in obeyed.result().answer

    plain = await Agent.open(client, sandbox=view.name, harness=harness, model=offered_model)
    with tracer.start_as_current_span(
        "system_test.session_instructions.plain_exchange", record_exception=False, set_status_on_exception=False
    ):
        async with asyncio.TaskGroup() as turns:
            turns.create_task(plain.run(PROMPT))
            request = await model.request()
            assert INSTRUCTIONS not in request.system_text, request.system_text
            await model.reply(request, Text("A checksum detects corrupted data."))


# `runner_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf
