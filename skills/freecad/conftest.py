"""Shared fixtures for FreeCAD tests."""

import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest
import pytest_bazel
from opentelemetry import trace

from util.bazel.runfiles import get_required_path
from util.bazel.subprocess import python_env
from util.testing.otel_tracing import configure_tracing

# CLEANUP(2026-04-10): Unused — tests migrated to conda fixtures. eval/run_eval.py
# has its own OciImage. Remove once confirmed no new Docker-based tests are planned.
# FREECAD_TEST = OciImage("_main/skills/freecad/freecad_test.rloc", "freecad-test:pinned")

_CONDA_ENV_REPO = "rules_conda++conda+freecad_conda"

# Anchor file inside the conda env — used to locate the conda root via Rlocation.
_CONDA_FREECADCMD_RLOCATION = f"{_CONDA_ENV_REPO}/bin/freecadcmd"

tracer = trace.get_tracer(__name__)


def pytest_configure(config: pytest.Config) -> None:
    configure_tracing(config)


# ---------------------------------------------------------------------------
# Conda env helpers
# ---------------------------------------------------------------------------


def _find_conda_root() -> Path:
    """Locate the conda env root via Rlocation of an anchor file inside the env."""
    anchor = get_required_path(_CONDA_FREECADCMD_RLOCATION)
    # anchor is <conda_root>/bin/freecadcmd → parent.parent is the conda root
    conda_root = anchor.parent.parent
    if not conda_root.is_dir():
        raise RuntimeError(f"Conda env not found at {conda_root}")
    return conda_root


def freecad_env(conda_root: Path, freecad_home: Path, *, display: str | None = None) -> dict[str, str]:
    """Complete environment for running conda FreeCAD as a subprocess.

    Args:
        conda_root: Path to the conda env root in runfiles.
        freecad_home: Hermetic home dir (prevents version-migration dialogs).
        display: X11 DISPLAY string (e.g. ':1'). If None, uses offscreen rendering.
    """
    env = {
        **os.environ,
        # Conda relocation — binary has paths baked from install machine
        "LD_LIBRARY_PATH": str(conda_root / "lib"),
        "PYTHONHOME": str(conda_root),
        "PATH": str(conda_root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        "QT_PLUGIN_PATH": str(conda_root / "lib" / "qt6" / "plugins"),
        # FreeCAD isolation
        "HOME": str(freecad_home),
        "FREECAD_USER_HOME": str(freecad_home),
    }
    if display:
        env["DISPLAY"] = display
        env["QT_QPA_PLATFORM"] = "xcb"
    else:
        env["QT_QPA_PLATFORM"] = "offscreen"
    # Suppress Wayland — force X11 or offscreen
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("GDK_BACKEND", None)
    return env


def _freecad_run(
    conda_binary: Path,
    script: Path,
    outdir: Path,
    env: dict[str, str],
    overrides: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    """Run a FreeCAD script as a subprocess with monorepo imports injected."""
    run_env = {**env, "OUTDIR": str(outdir)}
    if overrides:
        run_env.update(overrides)
    # Inject _main runfiles paths so FreeCAD scripts can import from the
    # monorepo. Filter out Bazel's Python 3.13 stdlib/site-packages — they
    # conflict with FreeCAD's bundled Python 3.14.
    base_pythonpath = python_env().get("PYTHONPATH", "")
    inject_paths = [p for p in base_pythonpath.split(os.pathsep) if p and "/_main" in p and "site-packages" not in p]
    wrapper = outdir / "_freecad_wrapper.py"
    wrapper.write_text(
        "import sys\n"
        f"for p in {inject_paths!r}:\n"
        "    if p not in sys.path:\n"
        "        sys.path.insert(0, p)\n"
        f"exec(open({str(script)!r}).read())\n"
    )
    result = subprocess.run(
        [conda_binary, wrapper], env=run_env, capture_output=True, text=True, timeout=timeout, check=False
    )
    wrapper.unlink(missing_ok=True)
    return result


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def assert_run_ok(result: subprocess.CompletedProcess, script_name: str, uo: Path, name: str) -> None:
    """Assert subprocess success, saving stdout/stderr to undeclared outputs for post-mortem."""
    if result.stdout:
        (uo / f"{name}.stdout").write_text(result.stdout)
    if result.stderr:
        (uo / f"{name}.stderr").write_text(result.stderr)
    assert result.returncode == 0, (
        f"{script_name} failed (exit {result.returncode}) — see {name}.stdout/.stderr in test outputs"
    )


def copy_outputs(src: Path, dst: Path) -> None:
    """Copy all files from src to dst for undeclared test outputs."""
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def conda_root() -> Path:
    """Locate the conda env root via Rlocation."""
    return _find_conda_root()


@pytest.fixture(scope="session")
def freecad_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Hermetic FreeCAD user home dir shared across the test session."""
    return tmp_path_factory.mktemp("freecad_home")


@pytest.fixture(scope="session")
def xvfb_display() -> Generator[str]:
    """Start a session-scoped Xvfb server. Yields the DISPLAY string (e.g. ':1')."""
    r_fd, w_fd = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(w_fd), "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(w_fd,),
    )
    os.close(w_fd)
    raw = os.read(r_fd, 16).decode().strip()
    os.close(r_fd)
    if proc.poll() is not None:
        raise RuntimeError(f"Xvfb exited immediately (returncode={proc.returncode})")
    display = f":{raw}"
    yield display
    proc.terminate()
    proc.wait()


def _make_runner(binary: Path, base_env: dict[str, str], default_timeout: int = 180):
    """Create a FreeCAD script runner closure."""

    def _run(
        script: Path, outdir: Path, env: dict | None = None, timeout: int = default_timeout
    ) -> subprocess.CompletedProcess:
        return _freecad_run(binary, script, outdir, base_env, env, timeout)

    return _run


@pytest.fixture(scope="session")
def freecad_gui(conda_root: Path, xvfb_display: str, freecad_home: Path):
    """Run a FreeCAD script under the GUI binary with Xvfb. Returns a callable."""
    return _make_runner(conda_root / "bin" / "freecad", freecad_env(conda_root, freecad_home, display=xvfb_display))


@pytest.fixture(scope="session")
def freecad_run(conda_root: Path, freecad_home: Path):
    """Run a FreeCAD script headlessly via freecadcmd (no Xvfb). Returns a callable."""
    return _make_runner(conda_root / "bin" / "freecadcmd", freecad_env(conda_root, freecad_home), default_timeout=120)


if __name__ == "__main__":
    pytest_bazel.main()
