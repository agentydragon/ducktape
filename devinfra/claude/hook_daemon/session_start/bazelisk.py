"""Install Bazel wrapper for proxy credential injection.

The wrapper script intercepts `bazel` invocations to inject proxy credentials
via RPC before calling the real bazelisk binary (provided by Nix via web-session).
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import ENV_SESSION_DIR
from util.bazel.subprocess import write_shell_wrapper

logger = logging.getLogger(__name__)


@dataclass
class BazeliskSetup:
    """Result of bazelisk wrapper installation."""

    bazelisk_path: Path
    wrapper_path: Path

    @property
    def status(self) -> str:
        """Get status string for logging."""
        bazel_on_path = shutil.which("bazel")
        if bazel_on_path and Path(bazel_on_path).resolve() == self.wrapper_path.resolve():
            return f"wrapper at {self.wrapper_path}"
        if self.wrapper_path.exists():
            return f"wrapper exists but not on PATH ({self.wrapper_path})"
        return "no wrapper"


def resolve_bazelisk() -> Path:
    """Find bazelisk on PATH (provided by Nix web-session package)."""
    bazelisk = shutil.which("bazelisk")
    if bazelisk:
        return Path(bazelisk)
    # Fallback: some setups put it as "bazel" directly
    bazel = shutil.which("bazel")
    if bazel:
        return Path(bazel)
    raise RuntimeError("bazelisk not found on PATH (expected from Nix web-session package)")


_WRAPPER_RUNTIME_LINES = (
    'export _BAZEL_WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd)"\nexport _BAZEL_WRAPPER_NAME="$(basename "$0")"'
)


def install_wrapper(paths: SessionPaths, *, wrapper_dir: Path | None = None) -> Path:
    """Install wrapper script that sets proxy env vars before calling bazelisk.

    Also creates a bazelisk symlink for pre-commit hooks.
    """
    if wrapper_dir is None:
        wrapper_dir = paths.wrapper_dir
    wrapper_path = wrapper_dir / "bazel"

    wrapper_dir.mkdir(parents=True, exist_ok=True)

    write_shell_wrapper(
        wrapper_path,
        "devinfra.claude.bazel_wrapper",
        baked_env={ENV_SESSION_DIR: str(paths.session_dir)},
        extra_lines=_WRAPPER_RUNTIME_LINES,
    )
    logger.info("Installed bazel wrapper at %s", wrapper_path)

    # Create bazelisk symlink for pre-commit hooks
    bazelisk_symlink = wrapper_dir / "bazelisk"
    if bazelisk_symlink.exists() or bazelisk_symlink.is_symlink():
        bazelisk_symlink.unlink()
    bazelisk_symlink.symlink_to(wrapper_path)
    logger.info("Created bazelisk symlink at %s", bazelisk_symlink)

    return wrapper_path
