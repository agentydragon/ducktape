"""Container E2E test: build wheel, install in container, run hook, bazel build.

Verifies the full wheel packaging and session start flow in an isolated Docker
container. Exercises the pip-install + session-start path end-to-end so that
wheel packaging bugs (missing deps, bad entry points, ImportError at runtime)
surface in CI rather than in a live session.

Current containers have direct internet via a transparent proxy — no egress
proxy setup. Earlier versions of this test stood up a mitmproxy sidecar and
an isolated Docker network to exercise the legacy `auth_proxy` subsystem;
that machinery was removed when we stopped supporting explicit
`HTTPS_PROXY`-based setups (see `devinfra/claude/README.md` "Historical:
Explicit Egress Proxy"). The test now runs against the default Docker bridge
with direct internet, same as real web containers.
"""

import json
import logging
import os
import shlex
import shutil
from pathlib import Path

import docker
import docker.models.containers
import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

# Wheels and bazelisk are baked into the e2e_container image via pkg_tar layers
# (see BUILD.bazel). Wheels at /wheel/, bazelisk at /tools/bazelisk (on PATH).
_WHEEL_DIR = "/wheel"

# Rlocation for a file in the test workspace (used to derive directory path)
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"

# E2E test container image (built by Bazel via rules_distroless, loaded via oci_image_info)
_E2E = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)

_CONTAINER_NAME = "ducktape-container-e2e"
_SESSION_ID = "container-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"


def _save_output(name: str, content: str) -> None:
    """Save content to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / "container-e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _exec(
    container: docker.models.containers.Container, cmd: list[str], *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a command in the container via docker exec.

    Returns (exit_code, stdout, stderr) as raw bytes. Raises AssertionError
    if check=True and the command fails.
    """
    result = container.exec_run(cmd, demux=True, workdir=workdir)
    stdout, stderr = result.output
    stdout = stdout or b""
    stderr = stderr or b""
    if check:
        assert result.exit_code == 0, (
            f"Command failed: {cmd!r}\nexit_code={result.exit_code}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\nstderr:\n{stderr.decode(errors='replace')}"
        )
    return result.exit_code, stdout, stderr


@pytest.fixture
def test_workspace_path() -> Path:
    """Resolve the test workspace directory from runfiles."""
    return get_required_path(_TEST_WORKSPACE_MODULE).parent


@pytest.fixture
def e2e_image() -> str:
    """Load the e2e container OCI image into Docker."""
    return load_oci_image(_E2E)


def test_container_e2e(tmp_path: Path, test_workspace_path: Path, e2e_image: str) -> None:
    """Full E2E: install wheel in container, run hook, exercise PATH shims + bazel."""
    # Copy files to a staging directory so Docker can mount real files
    # (runfiles may be symlinks that Docker cannot resolve in gVisor)
    staging = tmp_path / "staging"
    staging.mkdir()
    staged_workspace = staging / "test_workspace"
    shutil.copytree(test_workspace_path, staged_workspace)
    (staged_workspace / ".git").mkdir()  # pre-commit needs a git repo

    container_name = f"{_CONTAINER_NAME}-{os.getpid()}"
    env = {
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "profile.yaml",
    }

    container = docker.from_env().containers.run(
        e2e_image,
        command=["sleep", "infinity"],
        name=container_name,
        environment=env,
        volumes={str(staged_workspace): {"bind": "/project", "mode": "ro"}},
        detach=True,
    )

    session_dir = f"/root/.claude/session-env/{_SESSION_ID}"

    try:
        logger.info("Started test container %s", container_name)

        # Install claude_hooks wheel (baked into image at /wheel/).
        # Install local wheels by path to avoid PyPI name collision (a public
        # "claude-hooks" package exists on PyPI). Transitive deps are fetched
        # from PyPI via the default Docker bridge network.
        # TODO(container-e2e): Install via uv by reading .claude/settings.json
        # hook definition and piping the JSON into sh, instead of raw pip.
        logger.info("Installing wheel")
        _exec(container, ["ls", "-la", _WHEEL_DIR])
        _exec(
            container,
            [
                "pip",
                "install",
                "-v",
                "--break-system-packages",
                f"{_WHEEL_DIR}/ducktape_util-0.1.0-py3-none-any.whl",
                f"{_WHEEL_DIR}/claude_hooks-0.1.0-py3-none-any.whl",
            ],
        )
        _exec(container, ["which", "claude-hook"])

        # Run session start hook
        logger.info("Running claude-hook (session start)")
        hook_input = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": _SESSION_ID,
                "cwd": "/project",
                "transcript_path": "/tmp/transcript.json",
                "permission_mode": "default",
                "source": "startup",
                "model": "claude-sonnet-4-6",
            }
        )
        _exec(container, ["bash", "-c", f"echo {shlex.quote(hook_input)} | claude-hook"])

        # Verify env_exports from profile.yaml were applied to session env file
        logger.info("Verifying env_exports in session env file")
        rc, env_content, _ = _exec(container, ["cat", _ENV_FILE], check=False)
        if rc == 0:
            env_text = env_content.decode(errors="replace")
            assert "E2E_TEST_SECRET" in env_text, (
                f"Expected E2E_TEST_SECRET from profile env_exports in session env file, got:\n{env_text}"
            )
            logger.info("env_exports verified: E2E_TEST_SECRET found in session env file")

        # Verify PATH shims were installed and work.
        # Session start installs git/bazelisk/bazel/bb/bbr shims and adds
        # the shim dir to PATH in the env file. The git shim should intercept
        # `git`, report to the daemon, then exec the real git.
        logger.info("Testing git shim: passthrough")
        rc, stdout, _ = _exec(container, ["bash", "-c", f"source {_ENV_FILE} && git --version"])
        assert b"git version" in stdout, f"git shim did not exec real git: {stdout!r}"

        # Verify the git shim blocks dangerous commands (block_add_all=true in profile).
        logger.info("Testing git shim: blocks git add -A")
        rc, _, stderr = _exec(container, ["bash", "-c", f"source {_ENV_FILE} && git add -A"], check=False)
        assert rc != 0, "git add -A should be blocked by git shim"
        assert b"BLOCKED" in stderr, f"Expected BLOCKED message, got: {stderr!r}"

        # Run bazel build (fetches BCR modules directly over the Docker bridge)
        logger.info("Running bazel build")
        bazel_cmd = f"source {_ENV_FILE} && bazelisk build //:hello"
        _exec(container, ["bash", "-c", bazel_cmd], workdir="/project")

    finally:
        stdout_logs = container.logs(stdout=True, stderr=False)
        stderr_logs = container.logs(stdout=False, stderr=True)
        _save_output("container-stdout.log", stdout_logs.decode(errors="replace"))
        _save_output("container-stderr.log", stderr_logs.decode(errors="replace"))

        # Extract specific log files from the container. We don't bind-mount
        # the session dir because the container (root) creates bazel cache/install
        # files that are unreadable by the CI runner and break Bazel's output collection.
        for log_file in ["hook-daemon/daemon.log", "sessionstart-hook-0.sh", "supervisor/supervisord.log", "bazelrc"]:
            rc, content, _ = _exec(container, ["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                _save_output(log_file.replace("/", "-"), content.decode(errors="replace"))

        container.remove(force=True)


if __name__ == "__main__":
    pytest_bazel.main()
