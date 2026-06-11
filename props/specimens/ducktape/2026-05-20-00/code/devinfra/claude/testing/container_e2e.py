"""Shared helpers for container E2E tests (python wheel vs rust binary)."""

import contextlib
import io
import json
import os
import shlex
import tarfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import docker
import docker.models.containers
import pytest

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

WHEEL_DIR = "/wheel"
_RUST_BINARY_RLOC = "_main/devinfra/claude/claude_hook/claude_hook"
E2E_IMAGE = OciImage(
    "_main/devinfra/claude/hook_daemon/session_start/container_e2e/e2e_container.rloc", "e2e-container:pinned"
)


@dataclass
class E2EContainer:
    """Running Docker container with E2E test helpers bound to it."""

    _container: docker.models.containers.Container = field(repr=False)
    _log_prefix: str = field(repr=False)
    _session_id: str = field(repr=False)
    _extra_session_files: Sequence[str] = field(default=(), repr=False)
    _extra_rust_files: Sequence[str] = field(default=(), repr=False)

    def exec(self, cmd: list[str], *, workdir: str | None = None, check: bool = True) -> tuple[int, bytes, bytes]:
        result = self._container.exec_run(cmd, demux=True, workdir=workdir)
        stdout, stderr = result.output
        stdout = stdout or b""
        stderr = stderr or b""
        if check:
            assert result.exit_code == 0, (
                f"Command failed: {cmd!r}\nexit_code={result.exit_code}\n"
                f"stdout:\n{stdout.decode(errors='replace')}\nstderr:\n{stderr.decode(errors='replace')}"
            )
        return result.exit_code, stdout, stderr

    def cp(self, src_path: str, dest_path: str, *, mode: int = 0o755) -> None:
        """Copy a local file into the container via put_archive."""
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
        self._container.put_archive(str(dest.parent), buf)

    def install_python(self) -> None:
        self.exec(
            [
                "pip",
                "install",
                "-q",
                "--break-system-packages",
                "--find-links",
                WHEEL_DIR,
                f"{WHEEL_DIR}/ducktape_util-0.1.0-py3-none-any.whl",
                f"{WHEEL_DIR}/ducktape_git_hooks-0.1.0-py3-none-any.whl",
                f"{WHEEL_DIR}/claude_hooks-0.1.0-py3-none-any.whl",
            ]
        )

    def install_rust(self) -> None:
        rust_binary = get_required_path(_RUST_BINARY_RLOC)
        self.cp(str(rust_binary), "/usr/local/bin/claude-hook")

    def send_hook(self, payload: dict) -> dict:
        """Pipe a hook JSON payload to claude-hook stdin and return parsed JSON output."""
        _, stdout, _ = self.exec(["bash", "-c", f"echo {shlex.quote(json.dumps(payload))} | claude-hook"])
        return json.loads(stdout) if stdout.strip() else {}

    def poll_file(self, path: str, timeout: int = 15) -> None:
        """Poll from the test process until path exists in the container."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, _, _ = self.exec(["test", "-f", path], check=False)
            if rc == 0:
                return
            time.sleep(0.1)
        pytest.fail(f"Timed out waiting for {path}")

    def _collect_logs(self) -> None:
        _save_output(
            self._log_prefix,
            "container-stdout.log",
            self._container.logs(stdout=True, stderr=False).decode(errors="replace"),
        )
        _save_output(
            self._log_prefix,
            "container-stderr.log",
            self._container.logs(stdout=False, stderr=True).decode(errors="replace"),
        )
        session_dir = f"/root/.claude/session-env/{self._session_id}"
        for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log", *self._extra_session_files]:
            rc, content, _ = self.exec(["cat", f"{session_dir}/{log_file}"], check=False)
            if rc == 0:
                _save_output(self._log_prefix, log_file.replace("/", "-"), content.decode(errors="replace"))
        for log_file in ["daemon.log", "daemon.err.log", *self._extra_rust_files]:
            rc, content, _ = self.exec(["cat", f"/tmp/claude-hd/{self._session_id}/{log_file}"], check=False)
            if rc == 0:
                _save_output(self._log_prefix, f"rust-{log_file}", content.decode(errors="replace"))


def _save_output(prefix: str, name: str, content: str) -> None:
    out_dir = undeclared_outputs_dir() / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def load_e2e_image() -> str:
    return load_oci_image(E2E_IMAGE)


@pytest.fixture
def e2e_image() -> str:
    return load_e2e_image()


@contextlib.contextmanager
def run_e2e_container(
    image: str,
    name_prefix: str,
    env: dict[str, str],
    staged_project: Path,
    log_prefix: str,
    session_id: str,
    *,
    extra_session_files: Sequence[str] = (),
    extra_rust_files: Sequence[str] = (),
) -> Iterator[E2EContainer]:
    raw = docker.from_env().containers.run(
        image,
        command=["sleep", "infinity"],
        name=f"{name_prefix}-{os.getpid()}",
        environment=env,
        volumes={str(staged_project): {"bind": "/project", "mode": "ro"}},
        detach=True,
    )
    c = E2EContainer(
        _container=raw,
        _log_prefix=log_prefix,
        _session_id=session_id,
        _extra_session_files=extra_session_files,
        _extra_rust_files=extra_rust_files,
    )
    try:
        yield c
    finally:
        c._collect_logs()
        raw.remove(force=True)
