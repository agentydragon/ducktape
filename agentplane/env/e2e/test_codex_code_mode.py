"""Codex in code mode through the runner, on the catalog entry testing serves for GPT-6 Luna.

That bundled entry gives Codex one JavaScript `exec` tool in place of its function tools; the scripted model
answers a shell call with a script that runs the nested `exec_command`.
"""

from __future__ import annotations

import pytest
import pytest_bazel

from agentplane.env.e2e.testing import events
from agentplane.env.e2e.testing.driver import RunnerDriver
from agentplane.native.codex.testing import model_catalog
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import event_pb2, session_pb2
from agentplane.testing.scripted_model import ScriptedModel, ShellCall, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

pytestmark = pytest.mark.parametrize("harness", [session_pb2.HARNESS_CODEX], ids=["codex"])


@pytest.fixture(autouse=True)
def code_mode_catalog(endpoint: object) -> None:
    assert isinstance(endpoint, OpenAIResponses)
    endpoint.serve_catalog([model_catalog.bundled("gpt-6-luna", slug=MODEL)])


@pytest.mark.parametrize(
    ("command", "output", "succeeded"),
    [("printf TOOL_OUTPUT", "TOOL_OUTPUT", True), ("sh -c 'printf failing; exit 23'", "failing", False)],
    ids=["succeeds", "fails"],
)
async def test_shell_call_runs_through_exec(
    driver: RunnerDriver, model: ScriptedModel, spec: session_pb2.SessionSpec, command: str, output: str, succeeded: bool
) -> None:
    session = await driver.attach("code-mode-tool-1", spec=spec)
    await session.submit_input("input-1", "Run the tool and report TOOL_DONE.")

    request = await model.request()
    assert request.native.code_mode
    await model.reply(request, ShellCall("call_test_1", command))
    request = await model.request()
    (result,) = request.tool_outputs
    assert result.call_id == "call_test_1"
    # Codex's status header, then what the script passed to `text()`.
    _, emitted = result.blocks
    assert emitted == output
    await model.reply(request, Text("TOOL_DONE"))

    done = await session.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    seen = session.seen
    (call,) = events.items(seen, event_pb2.ITEM_KIND_TOOL_CALL)
    assert command in events.tool_arguments(seen, call)
    assert events.completed(seen, call).tool.succeeded == succeeded
    (text,) = events.items(seen, event_pb2.ITEM_KIND_ASSISTANT_TEXT)
    assert events.completed(seen, text).text == "TOOL_DONE"
    events.assert_sourced(seen)


if __name__ == "__main__":
    pytest_bazel.main()
