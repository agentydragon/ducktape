"""Generic PATH-intercepting shim — reports to daemon, resolves binary, execs.

All shim-specific logic (git blocking, bazelisk --bazelrc injection) lives
server-side. This is the shared runtime entrypoint for all PATH shims.

Invoked as `claude-hook shim <shim_name> [args...]` (new style, via the
profile-symlink binary that auto-follows nix profile refreshes) or via
`python -m devinfra.claude.hook_daemon.shim` with env vars (legacy style for
old-format shim wrappers written before this change).
"""

import logging
import os
import sys
from pathlib import Path

from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.hook_daemon.client import send_shim_exec
from devinfra.claude.hook_daemon.models import ShimBlocked, ShimExecRequest
from devinfra.claude.hook_daemon.shim_install import SHIM_NAME_ENV, SHIM_SESSION_ID_ENV, resolve_real_binary
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import ENV_SESSION_DIR

logger = logging.getLogger(__name__)


def _setup_logging(shim: str, paths: SessionPaths) -> None:
    """Configure logging to stderr and file."""
    formatter = logging.Formatter(f"[{shim}-shim] %(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.setLevel(logging.WARNING)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(stderr_handler)

    log_file = paths.sandbox_writable_dir / f"{shim}-shim.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    print(f"[{shim}-shim] log: {log_file}", file=sys.stderr)

    logger.info("%s shim started", shim)


def _report_shim(shim: str, session_id: str, paths: SessionPaths) -> list[str]:
    """Report to daemon, handle blocks, return argv to exec with.

    On block: prints message to stderr and exits 1.
    On daemon unreachable: logs error, returns original sys.argv (fallback).
    """
    report = ShimExecRequest(
        shim=shim, session_id=session_id, cwd=Path.cwd(), argv=sys.argv, pid=os.getpid(), env=dict(os.environ)
    )
    response = send_shim_exec(report, paths)
    if response is None:
        return sys.argv  # Fallback: daemon unreachable
    if isinstance(response, ShimBlocked):
        print(f"[{shim}-shim] BLOCKED: {response.message}", file=sys.stderr)
        raise SystemExit(1)
    return response.argv


def run_shim(shim_name: str, session_id: str) -> None:
    """Run the shim: report to daemon, resolve real binary, exec it.

    sys.argv must be set to [shim_name, <original args...>] before calling.
    Called from the `claude-hook shim` subcommand (new style) and from
    main() for legacy python -m invocations.
    """
    paths = SessionPaths.from_env(session_id, dict(os.environ))
    _setup_logging(shim_name, paths)
    log_entrypoint_debug(f"{shim_name}_shim")

    argv = _report_shim(shim_name, session_id, paths)

    # Propagate session dir to subprocesses of the real binary (e.g. bazel_wrapper).
    os.environ[ENV_SESSION_DIR] = str(paths.session_dir)

    try:
        real = resolve_real_binary(shim_name, paths.wrapper_dir)
    except FileNotFoundError:
        print(f"{shim_name}: command not found", file=sys.stderr)
        raise SystemExit(127)
    logger.info("Execing %s", real)
    os.execvp(real, [real, *argv[1:]])


def main() -> None:
    """Entry point for legacy python -m invocation (old-style shim wrappers)."""
    shim_name = os.environ.get(SHIM_NAME_ENV)
    session_id = os.environ.get(SHIM_SESSION_ID_ENV)
    if not shim_name or not session_id:
        raise RuntimeError(
            f"Shim env vars not set ({SHIM_NAME_ENV}, {SHIM_SESSION_ID_ENV}) "
            f"— shim must be installed via shim_install.install()"
        )
    run_shim(shim_name, session_id)


if __name__ == "__main__":
    main()
