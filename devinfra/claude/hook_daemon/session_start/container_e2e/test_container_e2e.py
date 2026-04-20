"""Container E2E test for the hook daemon's session-start flow.

Parameterized over two implementations:

  - ``python``: install the ``claude_hooks`` Python wheel (the current prod
    path).
  - ``rust``: copy the ``claude-hook`` Rust binary into ``/usr/local/bin``.

Both use the same container image, the same staged `/project` (real
`web_env.sh`, real SOPS fixtures, test profile, etc.), and the same
assertion set. Both implementations must pass the full contract.

Exercises the real secret-decryption + kubeconfig path end-to-end.
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
from devinfra.claude.testing import container_e2e
from util.bazel.runfiles import get_required_path

# Runfiles paths.
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"
_WEB_ENV_SH = "_main/devinfra/secrets/web_env.sh"
_COMMON_SH = "_main/devinfra/secrets/_common.sh"
_WRITE_KUBECONFIG = "_main/devinfra/claude/scripts/write_kubeconfig.py"
_TEST_PROFILE = "_main/devinfra/claude/hook_daemon/session_start/container_e2e/test_profile.yaml"
_SECRETS_DIR_MARKER = "_main/devinfra/claude/hook_daemon/testdata/e2e_secrets/test_age.key"

_TEST_SECRET_FILES = [
    "buildbuddy.yaml",
    "github-pat-agentydragon-agent.yaml",
    "github-ci-read-pat.yaml",
    "claude-web-k8s-cert.yaml",
]

_CONTAINER_NAME = "ducktape-container-e2e"
_SESSION_ID = "container-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"
# Daemon UDS lives under /tmp/claude-hd/<session_id>/ (AF_UNIX 108-byte limit).
_DAEMON_SOCK = f"/tmp/claude-hd/{_SESSION_ID}/d.sock"


def _exec_under_env(
    container: docker.models.containers.Container, shell_cmd: str, *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
    """Run a shell command after sourcing the session env file (agent's Bash behavior)."""
    return container_e2e.exec_in_container(
        container, ["bash", "-c", f"source {_ENV_FILE} && {shell_cmd}"], workdir=workdir, check=check
    )


# ---------------------------------------------------------------------------
# Parameterization: python (wheel install) vs rust (binary copy)
# ---------------------------------------------------------------------------


def _install_python_wheel(container: docker.models.containers.Container) -> None:
    container_e2e.install_python(container)


def _install_rust_binary(container: docker.models.containers.Container) -> None:
    container_e2e.install_rust(container)
    # write_kubeconfig.py (bg command) imports yaml; the Rust impl has no
    # Python runtime deps, so install PyYAML explicitly.
    container_e2e.exec_in_container(container, ["pip", "install", "--break-system-packages", "pyyaml"])


_IMPLS = {"python": _install_python_wheel, "rust": _install_rust_binary}


@pytest.fixture(params=list(_IMPLS.keys()))
def impl(request: pytest.FixtureRequest) -> str:
    param: str = request.param
    return param


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def staged_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()

    test_workspace = get_required_path(_TEST_WORKSPACE_MODULE).parent
    for src in test_workspace.iterdir():
        if src.name == "profile.yaml":
            continue
        if src.is_file():
            shutil.copy2(src, project / src.name)

    secrets_dir = project / "devinfra" / "secrets"
    secrets_dir.mkdir(parents=True)
    shutil.copy2(get_required_path(_WEB_ENV_SH), secrets_dir / "web_env.sh")
    shutil.copy2(get_required_path(_COMMON_SH), secrets_dir / "_common.sh")

    scripts_dir = project / "devinfra" / "claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(get_required_path(_WRITE_KUBECONFIG), scripts_dir / "write_kubeconfig.py")

    shutil.copy2(get_required_path(_TEST_PROFILE), project / "profile.yaml")
    secrets_fixtures = get_required_path(_SECRETS_DIR_MARKER).parent
    project_secrets = project / "secrets"
    project_secrets.mkdir()
    for name in _TEST_SECRET_FILES:
        shutil.copy2(secrets_fixtures / name, project_secrets / name)

    (project / ".git").mkdir()
    return project


@pytest.fixture
def test_age_key() -> str:
    raw = get_required_path(_SECRETS_DIR_MARKER).read_text()
    return next(line.strip() for line in raw.splitlines() if line.startswith("AGE-SECRET-KEY-"))


@pytest.fixture
def e2e_image() -> str:
    return container_e2e.load_e2e_image()


@pytest.fixture
def container(
    impl: str, staged_project: Path, test_age_key: str, e2e_image: str
) -> Iterator[docker.models.containers.Container]:
    env = {
        "CLAUDE_PROJECT_DIR": "/project",
        "CLAUDE_ENV_FILE": _ENV_FILE,
        "DUCKTAPE_CLAUDE_HOOKS_PROFILE": "profile.yaml",
        "SOPS_AGE_KEY": test_age_key,
    }
    c = docker.from_env().containers.run(
        e2e_image,
        command=["sleep", "infinity"],
        name=f"{_CONTAINER_NAME}-{impl}-{os.getpid()}",
        environment=env,
        volumes={str(staged_project): {"bind": "/project", "mode": "ro"}},
        detach=True,
    )
    prefix = f"container-e2e-{impl}"
    try:
        yield c
    finally:
        container_e2e.collect_container_logs(
            c,
            prefix,
            _SESSION_ID,
            extra_session_files=["sessionstart-hook-0.sh", "bazelrc"],
            extra_rust_files=["daemon.pid"],
        )
        c.remove(force=True)


# ---------------------------------------------------------------------------
# Contract test
# ---------------------------------------------------------------------------


def test_container_e2e(impl: str, container: docker.models.containers.Container) -> None:
    """SessionStart contract test, parameterized over python/rust impls."""
    # Install whichever claude-hook impl this run is for.
    _IMPLS[impl](container)
    container_e2e.exec_in_container(container, ["which", "claude-hook"])
    container_e2e.exec_in_container(container, ["which", "sops"])
    container_e2e.exec_in_container(container, ["which", "curl"])

    # Run SessionStart.
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
    container_e2e.exec_in_container(container, ["bash", "-c", f"echo {shlex.quote(hook_input)} | claude-hook"])

    # Env file exists.
    container_e2e.exec_in_container(container, ["test", "-f", _ENV_FILE])
    _, stdout, _ = _exec_under_env(container, "env -0")
    agent_env = parse_env_null_delimited(stdout)

    # env_exports from profile.
    assert agent_env["E2E_TEST_MARKER"] == "1"

    # Secrets decrypted by real web_env.sh.
    assert agent_env["BUILDBUDDY_API_KEY"] == "test-fake-bb-key"
    assert agent_env["GITHUB_TOKEN"] == "test-fake-gh-agent-token"
    assert agent_env["DUCKTAPE_CI_READ_GITHUB_TOKEN"] == "test-fake-ci-read-token"

    # Daemon alive on UDS.
    _, stdout, _ = container_e2e.exec_in_container(
        container, ["curl", "-sf", "--unix-socket", _DAEMON_SOCK, "http://localhost/health"]
    )
    assert b'"status":"ok"' in stdout

    # PATH shims: passthrough.
    _, stdout, _ = _exec_under_env(container, "git --version")
    assert b"git version" in stdout

    # Kubeconfig (written by bg command).
    container_e2e.poll_file(container, "/root/.kube/config", timeout=30)
    _, kubeconfig_raw, _ = container_e2e.exec_in_container(container, ["cat", "/root/.kube/config"])
    kube = yaml.safe_load(kubeconfig_raw)
    assert kube["current-context"] == "claude-code-web"
    user_data = kube["users"][0]["user"]
    assert "client-certificate-data" in user_data
    assert "client-key-data" in user_data
    assert kube["clusters"][0]["cluster"]["server"] == "https://api.allegedly.works"
    _, stat_out, _ = container_e2e.exec_in_container(container, ["stat", "-c", "%a", "/root/.kube/config"])
    assert stat_out.strip() == b"600"

    # git shim matrix: verify block vs passthrough decisions for a
    # representative set of commands. Runs in a real repo so passthrough
    # cases (git log, git status) succeed instead of failing on missing
    # HEAD. Block-expected cases short-circuit before real git runs so
    # they don't depend on repo state.
    container_e2e.exec_in_container(
        container,
        [
            "bash",
            "-c",
            "cd /tmp && git init -q shim-test && cd shim-test && "
            "git -c user.email=t@e.co -c user.name=t commit -q --allow-empty -m initial",
        ],
    )
    # (command, should_block)
    git_shim_tests = [
        ("git add -A", True),
        ("git add .", True),
        ("git add --all", True),
        ("git commit --amend --allow-empty -m x", True),
        ("git stash", True),
        ("git --version", False),
        ("git status", False),
        ("git log --oneline -1", False),
    ]
    for cmd, should_block in git_shim_tests:
        rc, _, stderr = _exec_under_env(container, cmd, workdir="/tmp/shim-test", check=False)
        stderr_str = stderr.decode(errors="replace")
        if should_block:
            assert rc != 0, f"[{impl}] {cmd!r} should have been blocked but exited 0\nstderr: {stderr_str}"
            assert b"BLOCKED" in stderr, f"[{impl}] {cmd!r} expected BLOCKED in stderr\nstderr: {stderr_str}"
        else:
            assert rc == 0, f"[{impl}] {cmd!r} should passthrough but exited {rc}\nstderr: {stderr_str}"
            assert b"BLOCKED" not in stderr, f"[{impl}] {cmd!r} unexpectedly BLOCKED\nstderr: {stderr_str}"

    # Bazel build over the staged workspace.
    _exec_under_env(container, "bazelisk build //:hello", workdir="/project")


if __name__ == "__main__":
    pytest_bazel.main()
