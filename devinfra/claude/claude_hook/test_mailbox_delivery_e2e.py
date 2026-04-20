"""Container E2E test: background task output delivered via REPL hook systemMessage.

Parameterized over two implementations:

  - ``python``: install the ``claude_hooks`` Python wheel.
  - ``rust``: copy the ``claude-hook`` Rust binary into ``/usr/local/bin``.

Scenario: a background command writes lines in two phases, gated by a
sentinel file. The test verifies that:

1. Output from the first phase appears in the systemMessage of the next
   REPL hook (PreToolUse), proving the daemon delivers buffered bg output.
2. A second PreToolUse does NOT re-deliver the first-phase output (drain is
   destructive).
3. After the second phase completes, output_Y appears in the next REPL hook.
"""

from collections.abc import Iterator
from pathlib import Path

import docker
import docker.models.containers
import pytest
import pytest_bazel

from devinfra.claude.testing import container_e2e
from util.bazel.runfiles import get_required_path

_TEST_PROFILE = "_main/devinfra/claude/claude_hook/mailbox_test_profile.yaml"
_SESSION_ID = "mailbox-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"
_CONTAINER_NAME = "ducktape-mailbox-e2e"

_IMPLS = {"python": container_e2e.install_python, "rust": container_e2e.install_rust}


@pytest.fixture(params=list(_IMPLS.keys()))
def impl(request: pytest.FixtureRequest) -> str:
    param: str = request.param
    return param


@pytest.fixture
def staged_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    profile_src = get_required_path(_TEST_PROFILE)
    (project / "mailbox_test_profile.yaml").write_bytes(profile_src.read_bytes())
    (project / ".git").mkdir()
    return project


@pytest.fixture
def container(impl: str, staged_project: Path, e2e_image: str) -> Iterator[docker.models.containers.Container]:
    env = {
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "mailbox_test_profile.yaml",
    }
    with container_e2e.run_e2e_container(
        e2e_image, f"{_CONTAINER_NAME}-{impl}", env, staged_project, f"mailbox-e2e-{impl}", _SESSION_ID
    ) as c:
        yield c


_SESSION_START = {
    "hook_event_name": "SessionStart",
    "session_id": _SESSION_ID,
    "cwd": "/project",
    "transcript_path": "/tmp/transcript.json",
    "permission_mode": "default",
    "source": "startup",
    "model": "claude-sonnet-4-6",
}

_PRE_TOOL_USE = {
    "hook_event_name": "PreToolUse",
    "session_id": _SESSION_ID,
    "cwd": "/project",
    "transcript_path": "/tmp/transcript.json",
    "permission_mode": "default",
    "tool_name": "Bash",
    "tool_use_id": "tool_001",
    "tool_input": {},
}


def test_mailbox_delivery(impl: str, container: docker.models.containers.Container) -> None:
    """Background task output is delivered once via REPL hook systemMessage, then drained."""
    _IMPLS[impl](container)
    container_e2e.send_hook(container, _SESSION_START)

    # The bg script prints output_X, sleeps 0.5s (giving the daemon's async
    # stdout reader time to buffer it), then creates task_ready. By the time
    # we see the sentinel, output_X is guaranteed in the daemon's buffer.
    container_e2e.poll_file(container, "/tmp/task_ready")

    out1 = container_e2e.send_hook(container, _PRE_TOOL_USE)
    msg1 = out1.get("systemMessage", "")
    assert "output_X" in msg1, f"[{impl}] expected output_X, got: {msg1!r}"

    # Drain is destructive: second hook must not re-deliver output_X.
    out2 = container_e2e.send_hook(container, _PRE_TOOL_USE)
    msg2 = out2.get("systemMessage") or ""
    assert "output_X" not in msg2, f"[{impl}] output_X re-delivered: {msg2!r}"

    container_e2e.exec_in_container(container, ["touch", "/tmp/signal"])
    container_e2e.poll_file(container, "/tmp/task_done")

    out3 = container_e2e.send_hook(container, _PRE_TOOL_USE)
    msg3 = out3.get("systemMessage", "")
    assert "output_Y" in msg3, f"[{impl}] expected output_Y, got: {msg3!r}"
    assert "output_X" not in msg3, f"[{impl}] output_X re-appeared in phase-2 message: {msg3!r}"


if __name__ == "__main__":
    pytest_bazel.main()
