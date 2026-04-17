"""Container E2E test for the hook daemon's session-start flow.

Exercises the real secret-decryption + kubeconfig path end-to-end inside an
isolated Docker container:

  - Stages a /project tree with the real devinfra/secrets/web_env.sh,
    the real _common.sh, a web-style profile (testdata/e2e_secrets/profile.yaml),
    and test-encrypted SOPS secrets (testdata/e2e_secrets/*.yaml) at the same
    repo-relative paths the real scripts expect.
  - Installs the claude_hooks wheel in the container.
  - Runs SessionStart. The daemon sources web_env.sh (decrypts the fake
    secrets with the test age key), writes ~/.kube/config via the real
    kubeconfig writer, and sets up PATH shims + bazelrc.
  - Asserts the agent's contract: after `source <ENV_FILE>`, BUILDBUDDY_API_KEY,
    GITHUB_TOKEN, DUCKTAPE_CI_READ_GITHUB_TOKEN are set; ~/.kube/config is a
    valid kubeconfig with the decrypted token; PATH shims work; bazel build
    succeeds.

The test age key is generated once and committed to testdata/e2e_secrets/test_age.key.
It only decrypts the fake secret fixtures in that directory; no real secrets
are involved.
"""

import json
import os
import shlex
import shutil
from collections.abc import Iterator
from pathlib import Path

import docker
import docker.models.containers
import pytest
import pytest_bazel
import yaml

from devinfra.claude.env_file import parse_env_null_delimited
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

# Wheels + tools (bazelisk, sops, curl) are baked into the e2e_container image
# via pkg_tar layers / the trixie_e2e apt manifest (see BUILD.bazel).
_WHEEL_DIR = "/wheel"

# Runfiles paths for real scripts + test fixtures.
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"
_WEB_ENV_SH = "_main/devinfra/secrets/web_env.sh"
_COMMON_SH = "_main/devinfra/secrets/_common.sh"
_FIXTURES_DIR_MARKER = "_main/devinfra/claude/hook_daemon/testdata/e2e_secrets/profile.yaml"

# Test SOPS files live at these repo-relative paths inside /project (matching
# what web_env.sh and write_kubeconfig_cli.py look for in the real repo).
_TEST_SECRET_FILES = [
    "buildbuddy.yaml",
    "github-pat-agentydragon-agent.yaml",
    "github-ci-read-pat.yaml",
    "claude-web-k8s-token.yaml",
]

_E2E = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)

_CONTAINER_NAME = "ducktape-container-e2e"
_SESSION_ID = "container-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"
_SESSION_DIR = f"/root/.claude/session-env/{_SESSION_ID}"
# Daemon UDS lives under /tmp/claude-hd/<session_id>/ (short path — AF_UNIX
# has a 108-byte sun_path limit; see devinfra/claude/session_paths.py).
_DAEMON_SOCK = f"/tmp/claude-hd/{_SESSION_ID}/d.sock"


def _save_output(name: str, content: str) -> None:
    """Save content to undeclared test outputs."""
    out_dir = undeclared_outputs_dir() / "container-e2e"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _exec(
    container: docker.models.containers.Container, cmd: list[str], *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a command in the container via docker exec."""
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


def _exec_under_env(
    container: docker.models.containers.Container, shell_cmd: str, *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a shell command inside the container after sourcing the session env file.

    Simulates what the agent's Bash tool does: Claude Code sources the session
    env file before every Bash call.
    """
    return _exec(container, ["bash", "-c", f"source {_ENV_FILE} && {shell_cmd}"], workdir=workdir, check=check)


@pytest.fixture
def staged_project(tmp_path: Path) -> Path:
    """A /project tree mirroring the real repo layout (for bind-mount into the container).

    /project/
      MODULE.bazel, BUILD.bazel, .bazelrc, .bazelversion   (test workspace)
      profile.yaml                                          (web-style test profile)
      devinfra/secrets/{web_env.sh,_common.sh}              (real scripts)
      secrets/{buildbuddy,github-pat-...,
               github-ci-read-pat,
               claude-web-k8s-token}.yaml                   (test-encrypted)
      .git/                                                 (pre-commit needs a repo)
    """
    project = tmp_path / "project"
    project.mkdir()

    test_workspace = get_required_path(_TEST_WORKSPACE_MODULE).parent
    for src in test_workspace.iterdir():
        if src.name == "profile.yaml":
            # Overridden by the e2e_secrets profile below.
            continue
        if src.is_file():
            shutil.copy2(src, project / src.name)

    # Real env scripts at their real repo paths.
    secrets_dir = project / "devinfra" / "secrets"
    secrets_dir.mkdir(parents=True)
    shutil.copy2(get_required_path(_WEB_ENV_SH), secrets_dir / "web_env.sh")
    shutil.copy2(get_required_path(_COMMON_SH), secrets_dir / "_common.sh")

    # Test profile + test-encrypted SOPS files (see testdata/e2e_secrets/).
    fixtures_dir = get_required_path(_FIXTURES_DIR_MARKER).parent
    shutil.copy2(fixtures_dir / "profile.yaml", project / "profile.yaml")
    project_secrets = project / "secrets"
    project_secrets.mkdir()
    for name in _TEST_SECRET_FILES:
        shutil.copy2(fixtures_dir / name, project_secrets / name)

    (project / ".git").mkdir()  # pre-commit wants a git repo

    return project


@pytest.fixture
def test_age_key() -> str:
    """Test-only age private key from testdata/e2e_secrets/test_age.key.

    Decrypts the fake SOPS fixtures in that directory; does not unlock any real
    secrets. Passed to the container as SOPS_AGE_KEY so the real web_env.sh and
    kubeconfig writer can read the fixtures.
    """
    fixtures_dir = get_required_path(_FIXTURES_DIR_MARKER).parent
    raw = (fixtures_dir / "test_age.key").read_text()
    # The file has a "# public key: ..." header and the AGE-SECRET-KEY-* line.
    return next(line.strip() for line in raw.splitlines() if line.startswith("AGE-SECRET-KEY-"))


@pytest.fixture
def e2e_image() -> str:
    """Load the e2e container OCI image into Docker."""
    return load_oci_image(_E2E)


@pytest.fixture
def container(staged_project: Path, test_age_key: str, e2e_image: str) -> Iterator[docker.models.containers.Container]:
    """Start the e2e container with the staged project bind-mounted at /project.

    Teardown saves container logs + daemon logs to undeclared outputs for
    post-mortem analysis, then force-removes the container.
    """
    env = {
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "profile.yaml",
        "SOPS_AGE_KEY": test_age_key,
    }
    c = docker.from_env().containers.run(
        e2e_image,
        command=["sleep", "infinity"],
        name=f"{_CONTAINER_NAME}-{os.getpid()}",
        environment=env,
        volumes={str(staged_project): {"bind": "/project", "mode": "ro"}},
        detach=True,
    )
    try:
        yield c
    finally:
        _save_output("container-stdout.log", c.logs(stdout=True, stderr=False).decode(errors="replace"))
        _save_output("container-stderr.log", c.logs(stdout=False, stderr=True).decode(errors="replace"))
        for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log", "sessionstart-hook-0.sh", "bazelrc"]:
            rc, content, _ = _exec(c, ["cat", f"{_SESSION_DIR}/{log_file}"], check=False)
            if rc == 0:
                _save_output(log_file.replace("/", "-"), content.decode(errors="replace"))
        c.remove(force=True)


def test_container_e2e(container: docker.models.containers.Container) -> None:
    """SessionStart contract test: secrets, kubeconfig, shims, bazel all work."""
    # Install claude_hooks wheel from the image's /wheel/ dir.
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
    _exec(container, ["which", "sops"])
    _exec(container, ["which", "curl"])

    # Run SessionStart hook.
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

    # ------------------------------------------------------------------
    # Agent contract assertions — what the agent's Bash tool calls see
    # after `source $CLAUDE_ENV_FILE`.
    # ------------------------------------------------------------------

    _exec(container, ["test", "-f", _ENV_FILE])
    _, stdout, _ = _exec_under_env(container, "env -0")
    agent_env = parse_env_null_delimited(stdout)

    # env_exports from profile.
    assert agent_env["E2E_TEST_MARKER"] == "1"

    # Secrets decrypted by real web_env.sh (_common.sh → BUILDBUDDY_API_KEY;
    # web_env.sh → GITHUB_TOKEN, DUCKTAPE_CI_READ_GITHUB_TOKEN).
    assert agent_env["BUILDBUDDY_API_KEY"] == "test-fake-bb-key"
    assert agent_env["GITHUB_TOKEN"] == "test-fake-gh-agent-token"
    assert agent_env["DUCKTAPE_CI_READ_GITHUB_TOKEN"] == "test-fake-ci-read-token"

    # Kubeconfig written by the real write_kubeconfig_cli code.
    _, kubeconfig_raw, _ = _exec(container, ["cat", "/root/.kube/config"])
    kube = yaml.safe_load(kubeconfig_raw)
    assert kube["current-context"] == "claude-code-web"
    assert kube["users"][0]["user"]["token"] == "test-fake-k8s-token"
    assert kube["clusters"][0]["cluster"]["server"] == "https://test.example/"
    # Kubeconfig must be 0o600 (contains a bearer token).
    _, stat_out, _ = _exec(container, ["stat", "-c", "%a", "/root/.kube/config"])
    assert stat_out.strip() == b"600"

    # Daemon alive on UDS.
    _, stdout, _ = _exec(container, ["curl", "-sf", "--unix-socket", _DAEMON_SOCK, "http://localhost/health"])
    assert b'"status":"ok"' in stdout

    # PATH shims: passthrough.
    _, stdout, _ = _exec_under_env(container, "git --version")
    assert b"git version" in stdout

    # git shim blocks `git add -A` (profile has block_add_all=true).
    rc, _, stderr = _exec_under_env(container, "git add -A", check=False)
    assert rc != 0
    assert b"BLOCKED" in stderr

    # Bazel build over the staged workspace.
    _exec_under_env(container, "bazelisk build //:hello", workdir="/project")


if __name__ == "__main__":
    pytest_bazel.main()
