"""Shared helpers for container E2E tests (python wheel vs rust binary)."""

import io
import tarfile
import time
from collections.abc import Sequence
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


def exec_in_container(
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


def docker_cp(
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


def install_python(container: docker.models.containers.Container) -> None:
    exec_in_container(
        container,
        [
            "pip",
            "install",
            "-q",
            "--break-system-packages",
            f"{WHEEL_DIR}/ducktape_util-0.1.0-py3-none-any.whl",
            f"{WHEEL_DIR}/claude_hooks-0.1.0-py3-none-any.whl",
        ],
    )


def install_rust(container: docker.models.containers.Container) -> None:
    rust_binary = get_required_path(_RUST_BINARY_RLOC)
    docker_cp(container, str(rust_binary), "/usr/local/bin/claude-hook")


def load_e2e_image() -> str:
    return load_oci_image(E2E_IMAGE)


def save_output(prefix: str, name: str, content: str) -> None:
    out_dir = undeclared_outputs_dir() / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / name).write_text(content)


def poll_file(container: docker.models.containers.Container, path: str, timeout: int = 15) -> None:
    """Poll from the test process until path exists in the container."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc, _, _ = exec_in_container(container, ["test", "-f", path], check=False)
        if rc == 0:
            return
        time.sleep(0.1)
    pytest.fail(f"Timed out waiting for {path}")


def collect_container_logs(
    container: docker.models.containers.Container,
    prefix: str,
    session_id: str,
    *,
    extra_session_files: Sequence[str] = (),
    extra_rust_files: Sequence[str] = (),
) -> None:
    """Collect container and daemon logs into undeclared test outputs."""
    save_output(prefix, "container-stdout.log", container.logs(stdout=True, stderr=False).decode(errors="replace"))
    save_output(prefix, "container-stderr.log", container.logs(stdout=False, stderr=True).decode(errors="replace"))
    session_dir = f"/root/.claude/session-env/{session_id}"
    for log_file in ["hook-daemon/daemon.log", "hook-daemon/daemon.err.log", *extra_session_files]:
        rc, content, _ = exec_in_container(container, ["cat", f"{session_dir}/{log_file}"], check=False)
        if rc == 0:
            save_output(prefix, log_file.replace("/", "-"), content.decode(errors="replace"))
    for log_file in ["daemon.log", "daemon.err.log", *extra_rust_files]:
        rc, content, _ = exec_in_container(container, ["cat", f"/tmp/claude-hd/{session_id}/{log_file}"], check=False)
        if rc == 0:
            save_output(prefix, f"rust-{log_file}", content.decode(errors="replace"))
