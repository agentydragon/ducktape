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

import io
import json
import os
import shlex
import shutil
import tarfile
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

# Runfiles paths.
_TEST_WORKSPACE_MODULE = "_main/devinfra/claude/testdata/test_workspace/MODULE.bazel"
_WEB_ENV_SH = "_main/devinfra/secrets/web_env.sh"
_COMMON_SH = "_main/devinfra/secrets/_common.sh"
_WRITE_KUBECONFIG = "_main/devinfra/claude/scripts/write_kubeconfig.py"
_TEST_PROFILE = "_main/devinfra/claude/hook_daemon/session_start/container_e2e/test_profile.yaml"
_SECRETS_DIR_MARKER = "_main/devinfra/claude/hook_daemon/testdata/e2e_secrets/test_age.key"
_RUST_BINARY = "_main/devinfra/claude/claude_hook/claude_hook"

_TEST_SECRET_FILES = [
    "buildbuddy.yaml",
    "github-pat-agentydragon-agent.yaml",
    "github-ci-read-pat.yaml",
    "claude-web-k8s-cert.yaml",
]

_E2E = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)

_CONTAINER_NAME = "ducktape-container-e2e"
_SESSION_ID = "container-e2e-test"
_ENV_FILE = f"/root/.claude/session-env/{_SESSION_ID}/sessionstart-hook-0.sh"
_SESSION_DIR = f"/root/.claude/session-env/{_SESSION_ID}"
# Daemon UDS lives under /tmp/claude-hd/<session_id>/ (AF_UNIX 108-byte limit).
_DAEMON_SOCK = f"/tmp/claude-hd/{_SESSION_ID}/d.sock"


def _save_output(impl: str, name: str, content: str) -> None:
    out_dir = undeclared_outputs_dir() / f"container-e2e-{impl}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def _exec(
    container: docker.models.containers.Container, cmd: list[str], *, workdir: str | None = None, check: bool = True
) -> tuple[int, bytes, bytes]:
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
    """Run a shell command after sourcing the session env file (agent's Bash behavior)."""
    return _exec(container, ["bash", "-c", f"source {_ENV_FILE} && {shell_cmd}"], workdir=workdir, check=check)


def _docker_cp(
    container: docker.models.containers.Container, src_path: str, dest_path: str, *, mode: int = 0o755
) -> None:
    """Copy a local file into a running container via put_archive."""
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


# ---------------------------------------------------------------------------
# Parameterization: python (wheel install) vs rust (binary copy)
# ---------------------------------------------------------------------------


def _install_python_wheel(container: docker.models.containers.Container) -> None:
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


def _install_rust_binary(container: docker.models.containers.Container) -> None:
    rust_binary = get_required_path(_RUST_BINARY)
    _docker_cp(container, str(rust_binary), "/usr/local/bin/claude-hook")
    # The Rust binary has no Python runtime deps; write_kubeconfig.py
    # (invoked as a bg command) imports yaml. The Python impl gets PyYAML
    # transitively via the claude_hooks wheel; the Rust impl must install
    # it explicitly.
    _exec(container, ["pip", "install", "--break-system-packages", "pyyaml"])


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
    return load_oci_image(_E2E)


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
    try:
        yield c
    finally:
        _save_output(impl, "container-stdout.log", c.logs(stdout=True, stderr=False).decode(errors="replace"))
        _save_output(impl, "container-stderr.log", c.logs(stdout=False, stderr=True).decode(errors="replace"))
        for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log", "sessionstart-hook-0.sh", "bazelrc"]:
            rc, content, _ = _exec(c, ["cat", f"{_SESSION_DIR}/{log_file}"], check=False)
            if rc == 0:
                _save_output(impl, log_file.replace("/", "-"), content.decode(errors="replace"))
        # Rust daemon writes logs under the short session dir.
        for log_file in ["daemon.log", "daemon.err.log", "daemon.pid"]:
            rc, content, _ = _exec(c, ["cat", f"/tmp/claude-hd/{_SESSION_ID}/{log_file}"], check=False)
            if rc == 0:
                _save_output(impl, f"rust-{log_file}", content.decode(errors="replace"))
        c.remove(force=True)


# ---------------------------------------------------------------------------
# Contract test
# ---------------------------------------------------------------------------


def test_container_e2e(impl: str, container: docker.models.containers.Container) -> None:
    """SessionStart contract test, parameterized over python/rust impls."""
    # Install whichever claude-hook impl this run is for.
    _IMPLS[impl](container)
    _exec(container, ["which", "claude-hook"])
    _exec(container, ["which", "sops"])
    _exec(container, ["which", "curl"])

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
    _exec(container, ["bash", "-c", f"echo {shlex.quote(hook_input)} | claude-hook"])

    # Env file exists.
    _exec(container, ["test", "-f", _ENV_FILE])
    _, stdout, _ = _exec_under_env(container, "env -0")
    agent_env = parse_env_null_delimited(stdout)

    # env_exports from profile.
    assert agent_env["E2E_TEST_MARKER"] == "1"

    # Secrets decrypted by real web_env.sh.
    assert agent_env["BUILDBUDDY_API_KEY"] == "test-fake-bb-key"
    assert agent_env["GITHUB_TOKEN"] == "test-fake-gh-agent-token"
    assert agent_env["DUCKTAPE_CI_READ_GITHUB_TOKEN"] == "test-fake-ci-read-token"

    # Daemon alive on UDS.
    _, stdout, _ = _exec(container, ["curl", "-sf", "--unix-socket", _DAEMON_SOCK, "http://localhost/health"])
    assert b'"status":"ok"' in stdout

    # PATH shims: passthrough.
    _, stdout, _ = _exec_under_env(container, "git --version")
    assert b"git version" in stdout

    # Kubeconfig (written by bg command).
    _exec(
        container, ["bash", "-c", "for i in $(seq 60); do [ -f /root/.kube/config ] && exit 0; sleep 0.5; done; exit 1"]
    )
    _, kubeconfig_raw, _ = _exec(container, ["cat", "/root/.kube/config"])
    kube = yaml.safe_load(kubeconfig_raw)
    assert kube["current-context"] == "claude-code-web"
    user_data = kube["users"][0]["user"]
    assert "client-certificate-data" in user_data
    assert "client-key-data" in user_data
    assert kube["clusters"][0]["cluster"]["server"] == "https://api.allegedly.works"
    _, stat_out, _ = _exec(container, ["stat", "-c", "%a", "/root/.kube/config"])
    assert stat_out.strip() == b"600"

    # git shim blocks `git add -A`.
    rc, _, stderr = _exec_under_env(container, "git add -A", check=False)
    assert rc != 0
    assert b"BLOCKED" in stderr

    # Bazel build over the staged workspace.
    _exec_under_env(container, "bazelisk build //:hello", workdir="/project")


if __name__ == "__main__":
    pytest_bazel.main()
