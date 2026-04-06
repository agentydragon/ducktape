"""Shared fixtures for FreeCAD tests."""

import os
import subprocess
import time
from pathlib import Path

import pytest
import pytest_bazel
from opentelemetry import trace

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.otel_tracing import configure_tracing, export_traces

# Docker-based test image (used by tests that still run in containers)
FREECAD_TEST = OciImage("_main/skills/freecad/freecad_test.rloc", "freecad-test:pinned")
FREECAD_HELPERS = "_main/skills/freecad/freecad_helpers.py"

# AppImage-based test fixtures
_FREECAD_APPIMAGE_RLOC = "_main/skills/freecad/freecad_appimage.rloc"

tracer = trace.get_tracer(__name__)


def pytest_configure(config: pytest.Config) -> None:
    configure_tracing(config)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    export_traces(session.config)


# ── Docker-based fixtures (legacy; kept for tests not yet converted) ──────────


@pytest.fixture(scope="session")
def freecad_image() -> str:
    """Load FreeCAD test image into Docker daemon and return its tag."""
    return load_oci_image(FREECAD_TEST)


def freecad_exec(container: LoggedContainer, cmd: str) -> None:
    """Run a command in a FreeCAD container, asserting success."""
    with tracer.start_as_current_span("freecad_exec", attributes={"cmd": cmd}):
        result = container.exec(cmd)
        output = result.output.decode(errors="replace")
        print(output)
        assert result.exit_code == 0, f"Command failed (exit {result.exit_code}): {output[:500]}"


# ── AppImage-based fixtures ───────────────────────────────────────────────────


@pytest.fixture(scope="session")
def freecad_appimage_path() -> Path:
    """Resolve the FreeCAD AppImage from Bazel runfiles."""
    rloc_file = get_required_path(_FREECAD_APPIMAGE_RLOC)
    return get_required_path(rloc_file.read_text().strip())


@pytest.fixture(scope="session")
def xvfb_display() -> pytest.FixtureRequest:
    """Start a session-scoped Xvfb server. Yields the DISPLAY string (e.g. ':99')."""
    display = ":99"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)  # wait for Xvfb to accept connections
    yield display
    proc.terminate()
    proc.wait()


@pytest.fixture(scope="session")
def freecad_gui(freecad_appimage_path: Path, xvfb_display: str):
    """Run a FreeCAD script under the GUI binary with Xvfb. Returns a callable.

    Usage: result = freecad_gui(script, outdir=Path(...), env={...})
    """

    def _run(
        script: Path, outdir: Path | None = None, env: dict | None = None, timeout: int = 180
    ) -> subprocess.CompletedProcess:
        run_env = {**os.environ, "DISPLAY": xvfb_display}
        if outdir is not None:
            run_env["OUTDIR"] = str(outdir)
        if env:
            run_env.update(env)
        return subprocess.run(
            [str(freecad_appimage_path), "freecad", str(script)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run


@pytest.fixture(scope="session")
def freecad_headless(freecad_appimage_path: Path):
    """Run a FreeCAD script headlessly via freecadcmd (no Xvfb needed). Returns a callable.

    Works for scripts that don't use TechDraw GUI exports or OpenGL rendering.
    Usage: result = freecad_headless(script, outdir=Path(...), env={...})
    """

    def _run(
        script: Path, outdir: Path | None = None, env: dict | None = None, timeout: int = 120
    ) -> subprocess.CompletedProcess:
        run_env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        if outdir is not None:
            run_env["OUTDIR"] = str(outdir)
        if env:
            run_env.update(env)
        return subprocess.run(
            [str(freecad_appimage_path), "freecadcmd", str(script)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    return _run


if __name__ == "__main__":
    pytest_bazel.main()
