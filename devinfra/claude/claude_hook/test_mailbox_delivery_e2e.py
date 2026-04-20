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

import io
import json
import os
import shlex
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.models.containers
import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

# Reuse the e2e container built for the session-start tests.
_E2E = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)

_WHEEL_DIR = "/wheel"
_RUST_BINARY = "_main/devinfra/claude/claude_hook/claude_hook"
_TEST_PROFILE = "_main/devinfra/claude/claude_hook/mailbox_test_profile.yaml"

_SESSION_ID = "mailbox-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"

_CONTAINER_NAME = "ducktape-mailbox-e2e"


def _save_output(impl: str, name: str, content: str) -> None:
    out_dir = undeclared_outputs_dir() / f"mailbox-e2e-{impl}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _exec(
    container: docker.models.containers.Container, cmd: list[str], *, check: bool = True
) -> tuple[int, bytes, bytes]:
    result = container.exec_run(cmd, demux=True)
    stdout, stderr = result.output
    stdout = stdout or b""
    stderr = stderr or b""
    if check:
        assert result.exit_code == 0, (
            f"Command failed: {cmd!r}\nexit_code={result.exit_code}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\nstderr:\n{stderr.decode(errors='replace')}"
        )
    return result.exit_code, stdout, stderr


def _docker_cp(
    container: docker.models.containers.Container, src_path: str, dest_path: str, *, mode: int = 0o755
) -> None:
    dest = Path(dest_path)
    with Path(src_path).open("rb") as f:
        data = f.read()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=dest.name)
        info.size = len(data)
        info.mode = mode
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive(str(dest.parent), buf)


def _send_hook(container: docker.models.containers.Container, payload: dict) -> dict:
    """Send a hook by piping JSON to claude-hook on stdin (real Claude Code invocation style)."""
    json_str = json.dumps(payload)
    _, stdout, _ = _exec(container, ["bash", "-c", f"echo {shlex.quote(json_str)} | claude-hook"])
    return json.loads(stdout) if stdout.strip() else {}


def _poll_file(container: docker.models.containers.Container, path: str, timeout: int = 15) -> None:
    """Wait for a file to appear; fail if it never does within timeout seconds."""
    _exec(
        container,
        [
            "bash",
            "-c",
            f"for i in $(seq {timeout * 10}); do [ -f {path} ] && exit 0; sleep 0.1; done; "
            f"echo 'Timed out waiting for {path}' >&2; exit 1",
        ],
    )


def _poll_hook_output(
    container: docker.models.containers.Container,
    payload: dict,
    expected: str,
    *,
    max_attempts: int = 20,
    delay: float = 0.5,
) -> dict:
    """Send REPL hook repeatedly until systemMessage contains expected string."""
    for _ in range(max_attempts):
        out = _send_hook(container, payload)
        if expected in (out.get("systemMessage") or ""):
            return out
        time.sleep(delay)
    msg = (out.get("systemMessage") or "") if "out" in dir() else ""
    pytest.fail(f"Timed out waiting for {expected!r} in systemMessage (last: {msg!r})")


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


def _install_python(container: docker.models.containers.Container) -> None:
    _exec(
        container,
        [
            "pip",
            "install",
            "-q",
            "--break-system-packages",
            f"{_WHEEL_DIR}/ducktape_util-0.1.0-py3-none-any.whl",
            f"{_WHEEL_DIR}/claude_hooks-0.1.0-py3-none-any.whl",
        ],
    )


def _install_rust(container: docker.models.containers.Container) -> None:
    rust_binary = get_required_path(_RUST_BINARY)
    _docker_cp(container, str(rust_binary), "/usr/local/bin/claude-hook")


_IMPLS = {"python": _install_python, "rust": _install_rust}


@pytest.fixture(params=list(_IMPLS.keys()))
def impl(request: pytest.FixtureRequest) -> str:
    param: str = request.param
    return param


# ---------------------------------------------------------------------------
# Container fixture
# ---------------------------------------------------------------------------


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
    return load_oci_image(_E2E)


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
    try:
        yield c
    finally:
        _save_output(impl, "container-stdout.log", c.logs(stdout=True, stderr=False).decode(errors="replace"))
        _save_output(impl, "container-stderr.log", c.logs(stdout=False, stderr=True).decode(errors="replace"))
        session_dir = f"/root/.claude/session-env/{_SESSION_ID}"
        for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log"]:
            rc, content, _ = _exec(c, ["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                _save_output(impl, log_file.replace("/", "-"), content.decode(errors="replace"))
        for log_file in ["daemon.log", "daemon.err.log"]:
            rc, content, _ = _exec(c, ["cat", f"/tmp/claude-hd/{_SESSION_ID}/{log_file}"], check=False)
            if rc == 0:
                _save_output(impl, f"rust-{log_file}", content.decode(errors="replace"))
        c.remove(force=True)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

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

    # Start the daemon; background command begins running.
    _send_hook(container, _SESSION_START)

    # Phase 1: wait for task_ready sentinel. The profile sleeps 0.5s before
    # touching it, giving the daemon's async stdout reader time to buffer output_X.
    _poll_file(container, "/tmp/task_ready")

    # First REPL hook: drain produces output_X from bg buffer.
    out1 = _poll_hook_output(container, _PRE_TOOL_USE, "output_X")
    msg1 = out1.get("systemMessage", "")
    assert "output_X" in msg1, f"[{impl}] expected output_X, got: {msg1!r}"

    # Second REPL hook immediately after: output_X must NOT reappear (drain is destructive).
    out2 = _send_hook(container, _PRE_TOOL_USE)
    msg2 = out2.get("systemMessage") or ""
    assert "output_X" not in msg2, f"[{impl}] output_X re-delivered: {msg2!r}"

    # Phase 2: ungate the bg command and wait for completion.
    _exec(container, ["touch", "/tmp/signal"])
    _poll_file(container, "/tmp/task_done")

    # Third REPL hook: drain produces output_Y.
    out3 = _poll_hook_output(container, _PRE_TOOL_USE, "output_Y")
    msg3 = out3.get("systemMessage", "")
    assert "output_Y" in msg3, f"[{impl}] expected output_Y, got: {msg3!r}"
    assert "output_X" not in msg3, f"[{impl}] output_X re-appeared in phase-2 message: {msg3!r}"


if __name__ == "__main__":
    pytest_bazel.main()
