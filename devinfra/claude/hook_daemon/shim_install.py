"""Shim installation — installs PATH-intercepting shell shims during session start."""

import logging
import os
import shlex
from pathlib import Path

from devinfra.claude.session_paths import SessionPaths

logger = logging.getLogger(__name__)

# Shim-internal env vars (baked into shell wrappers at install time).
# Double-underscore prefix = private, not part of the public DUCKTAPE_CLAUDE_HOOKS_* namespace.
SHIM_DIR_ENV = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_DIR"
SHIM_NAME_ENV = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_NAME"
SHIM_SESSION_ID_ENV = "__DUCKTAPE_CLAUDE_HOOKS_SHIM_SESSION_ID"


def install(shim_name: str, paths: SessionPaths) -> Path:
    """Install a shell shim at paths.wrapper_dir/<shim_name>."""
    wrapper_dir = paths.wrapper_dir
    shim_path = wrapper_dir / shim_name

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    # `exec claude-hook` resolves via PATH at wrapper exec time, not at install
    # time, so `nix profile install` (or home-manager switch) takes effect for
    # all subsequent shim invocations without rewriting shims or restarting the
    # session.  Only the session ID is baked in; everything else is derived.
    content = (
        "#!/bin/sh\n"
        f"export {SHIM_SESSION_ID_ENV}={shlex.quote(paths.session_id)}\n"
        f'exec claude-hook shim {shlex.quote(shim_name)} "$@"\n'
    )
    shim_path.write_text(content)
    shim_path.chmod(0o755)
    logger.info("Installed %s shim at %s", shim_name, shim_path)

    return shim_path


def resolve_real_binary(binary_name: str, shim_dir: Path | None = None) -> str:
    """Find the real binary on PATH, skipping the shim directory.

    shim_dir: directory to exclude (the session wrapper_dir). When None, falls
    back to the legacy SHIM_DIR_ENV env var set by old-style shim wrappers.
    """
    if shim_dir is None:
        shim_dir_str = os.environ.get(SHIM_DIR_ENV, "")
        shim_dir = Path(shim_dir_str) if shim_dir_str else None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if shim_dir and Path(directory).resolve() == shim_dir.resolve():
            continue
        candidate = Path(directory) / binary_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(f"No {binary_name} found on PATH (outside shim directory)")
