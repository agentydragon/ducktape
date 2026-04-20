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

import json
import os
import shlex
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
def e2e_image() -> str:
    return container_e2e.load_e2e_image()


@pytest.fixture
def container(impl: str, staged_project: Path, e2e_image: str) -> Iterator[docker.models.containers.Container]:
    env = {
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "mailbox_test_profile.yaml",
    }
    c = docker.from_env().containers.run(
        e2e_image,
        command=["sleep", "infinity"],
        name=f"{_CONTAINER_NAME}-{impl}-{os.getpid()}",
        environment=env,
        volumes={str(staged_project): {"bind": "/project", "mode": "ro"}},
        detach=True,
    )
    prefix = f"mailbox-e2e-{impl}"
    try:
        yield c
    finally:
        container_e2e.save_output(
            prefix, "container-stdout.log", c.logs(stdout=True, stderr=False).decode(errors="replace")
        )
        container_e2e.save_output(
            prefix, "container-stderr.log", c.logs(stdout=False, stderr=True).decode(errors="replace")
        )
        session_dir = f"/root/.claude/session-env/{_SESSION_ID}"
        for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log"]:
            rc, content, _ = container_e2e.exec_in_container(c, ["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                container_e2e.save_output(prefix, log_file.replace("/", "-"), content.decode(errors="replace"))
        for log_file in ["daemon.log", "daemon.err.log"]:
            rc, content, _ = container_e2e.exec_in_container(
                c, ["cat", f"/tmp/claude-hd/{_SESSION_ID}/{log_file}"], check=False
            )
            if rc == 0:
                container_e2e.save_output(prefix, f"rust-{log_file}", content.decode(errors="replace"))
        c.remove(force=True)


def _send_hook(container: docker.models.containers.Container, payload: dict) -> dict:
    """Send a hook by piping JSON to claude-hook on stdin (real Claude Code invocation style)."""
    json_str = json.dumps(payload)
    _, stdout, _ = container_e2e.exec_in_container(
        container, ["bash", "-c", f"echo {shlex.quote(json_str)} | claude-hook"]
    )
    return json.loads(stdout) if stdout.strip() else {}


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
    _send_hook(container, _SESSION_START)

    # The bg script prints output_X, sleeps 0.5s (giving the daemon's async
    # stdout reader time to buffer it), then creates task_ready. By the time
    # we see the sentinel, output_X is guaranteed in the daemon's buffer.
    container_e2e.poll_file(container, "/tmp/task_ready")

    out1 = _send_hook(container, _PRE_TOOL_USE)
    msg1 = out1.get("systemMessage", "")
    assert "output_X" in msg1, f"[{impl}] expected output_X, got: {msg1!r}"

    # Drain is destructive: second hook must not re-deliver output_X.
    out2 = _send_hook(container, _PRE_TOOL_USE)
    msg2 = out2.get("systemMessage") or ""
    assert "output_X" not in msg2, f"[{impl}] output_X re-delivered: {msg2!r}"

    container_e2e.exec_in_container(container, ["touch", "/tmp/signal"])
    container_e2e.poll_file(container, "/tmp/task_done")

    out3 = _send_hook(container, _PRE_TOOL_USE)
    msg3 = out3.get("systemMessage", "")
    assert "output_Y" in msg3, f"[{impl}] expected output_Y, got: {msg3!r}"
    assert "output_X" not in msg3, f"[{impl}] output_X re-appeared in phase-2 message: {msg3!r}"


if __name__ == "__main__":
    pytest_bazel.main()
