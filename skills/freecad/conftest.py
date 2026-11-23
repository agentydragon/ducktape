"""Shared fixtures for FreeCAD tests."""

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from opentelemetry import trace
from python.runfiles import Runfiles

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.otel_tracing import configure_tracing

_CONDA_ENV_REPO = "rules_conda++conda+freecad_conda"

# Anchor file inside the conda env — used to locate the conda root via Rlocation.
_CONDA_FREECADCMD_RLOCATION = f"{_CONDA_ENV_REPO}/bin/freecadcmd"

tracer = trace.get_tracer(__name__)


def _write_fontconfig(conda_root: Path, freecad_home: Path) -> Path:
    """Create a fontconfig file that exposes only FreeCAD's bundled TechDraw font."""
    font_dir = conda_root / "share" / "Mod" / "TechDraw" / "Resources" / "fonts"
    if not font_dir.is_dir():
        raise RuntimeError(f"FreeCAD TechDraw font directory not found: {font_dir}")

    config_path = freecad_home / "fonts.conf"
    config_path.write_text(
        f"""<?xml version=\"1.0\"?>
<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">
<fontconfig>
  <dir>{font_dir}</dir>
  <alias>
    <family>sans-serif</family>
    <prefer><family>osifont</family></prefer>
  </alias>
  <alias>
    <family>Sans Serif</family>
    <prefer><family>osifont</family></prefer>
  </alias>
  <alias>
    <family>serif</family>
    <prefer><family>osifont</family></prefer>
  </alias>
  <alias>
    <family>monospace</family>
    <prefer><family>osifont</family></prefer>
  </alias>
</fontconfig>
"""
    )
    return config_path


def pytest_configure() -> None:
    configure_tracing()


def _find_conda_root() -> Path:
    """Locate the conda env root via Rlocation of an anchor file inside the env."""
    anchor = Runfiles.CreateOrRaise().root() / _CONDA_FREECADCMD_RLOCATION
    # anchor is <conda_root>/bin/freecadcmd → parent.parent is the conda root
    conda_root = Path(str(anchor)).parent.parent
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
        # FreeCAD isolation. A custom FREECAD_USER_HOME also skips DlgVersionMigrator: on a fresh default
        # home its modal dialog is invisible offscreen and blocks Gui.showMainWindow() forever.
        "HOME": str(freecad_home),
        "FREECAD_USER_HOME": str(freecad_home),
        # Do not inherit the RBE worker's system font inventory.  The worker
        # image may change generic-family resolution (for example when a new
        # Liberation package is installed), which changes TechDraw text
        # metrics.  Use only the font bundled with the pinned FreeCAD env.
        "FONTCONFIG_FILE": str(_write_fontconfig(conda_root, freecad_home)),
        "FONTCONFIG_PATH": str(freecad_home),
        "XDG_CACHE_HOME": str(freecad_home / ".cache"),
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
    # Inject this repository's runfiles paths so FreeCAD scripts can import from it.
    # Bazel's Python site-packages conflict with FreeCAD's bundled Python.
    repo_root = Path(__file__).absolute().parents[2]
    inject_paths = [
        p
        for p in dict.fromkeys([*sys.path, *os.environ.get("PYTHONPATH", "").split(os.pathsep)])
        if p and Path(p).is_relative_to(repo_root) and "site-packages" not in Path(p).parts
    ]
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


@dataclass(frozen=True)
class Stage:
    """One FreeCAD script run of a golden test."""

    script: str  # runfiles path
    label: str  # stem of the stdout/stderr files saved as undeclared outputs
    gui: bool = True  # False runs headless with freecadcmd


@dataclass(frozen=True)
class StageRunner:
    freecad_run: Callable[..., subprocess.CompletedProcess]
    freecad_gui: Callable[..., subprocess.CompletedProcess]
    runfiles: Runfiles
    tmp_path_factory: pytest.TempPathFactory

    def run(self, name: str, *stages: Stage, fcstd: str) -> Path:
        """Run `stages` in order in a fresh directory and return it.

        The first stage must produce `fcstd`; later stages get it as `INPUT`. Each stage's
        stdout/stderr and the final directory contents are saved under undeclared outputs `name`.
        """
        out_dir = self.tmp_path_factory.mktemp(name)
        uo = undeclared_outputs_dir() / name
        uo.mkdir(parents=True, exist_ok=True)

        env: dict[str, str] | None = None
        for stage in stages:
            runner = self.freecad_gui if stage.gui else self.freecad_run
            result = runner(self.runfiles.root() / stage.script, outdir=out_dir, env=env)
            assert_run_ok(result, Path(stage.script).name, uo, stage.label)
            if env is None:
                produced = out_dir / fcstd
                assert produced.exists(), f"{fcstd} not produced — see {stage.label}.stderr in test outputs"
                env = {"INPUT": str(produced)}

        copy_outputs(out_dir, uo)
        return out_dir


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


@pytest.fixture(scope="session")
def stage_runner(freecad_run, freecad_gui, runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory) -> StageRunner:
    """Run a golden test's FreeCAD scripts in order into one output directory."""
    return StageRunner(freecad_run, freecad_gui, runfiles, tmp_path_factory)
