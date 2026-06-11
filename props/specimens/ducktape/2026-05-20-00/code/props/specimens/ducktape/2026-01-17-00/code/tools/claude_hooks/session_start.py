"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up Bazel proxy and git hooks.
CLI mode: Loads direnv environment.
"""

from __future__ import annotations

import importlib.resources
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import textwrap
import traceback
from datetime import datetime
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from claude_hooks import bazelisk_setup, binary_tools, nix_setup, proxy_setup, supervisor_setup
from claude_hooks.errors import DirenvError, ProjectNotFoundError

CACHE_DIR = Path.home() / ".cache" / "claude-code-web"
LOG_FILE = CACHE_DIR / "session-start.log"
TIMESTAMP_FILE = CACHE_DIR / "session-hook-last-run"

logger = logging.getLogger(__name__)


class HookInput(BaseModel):
    """Input passed to Claude Code hooks via stdin."""

    session_id: str
    cwd: Path
    transcript_path: str
    permission_mode: Literal["default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"]
    hook_event_name: Literal["SessionStart"]
    source: Literal["startup", "resume", "clear", "compact"]


# ============================================================================
# CLI mode: direnv environment loading
# ============================================================================


def find_envrc(start_dir: Path) -> Path | None:
    """Walk up from start_dir to find .envrc file."""
    current = start_dir.resolve()
    while current != current.parent:
        envrc = current / ".envrc"
        if envrc.exists():
            return envrc
        current = current.parent
    return None


def run_cli_mode(hook_input: HookInput) -> None:
    """CLI mode: load direnv environment."""
    # Find .envrc (walk up from cwd)
    envrc = find_envrc(hook_input.cwd)
    if not envrc:
        # Fallback to ducktape root
        ducktape_envrc = Path.home() / "code" / "ducktape" / ".envrc"
        if ducktape_envrc.exists():
            envrc = ducktape_envrc
        else:
            return  # No .envrc to load

    # Print direnv-style loading banner
    print(f"direnv: loading {envrc}")

    # Use direnv to export the environment
    try:
        result = subprocess.run(
            ["direnv", "export", "bash"], check=False, cwd=envrc.parent, capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        print("direnv: not installed, skipping", file=sys.stderr)
        return
    except subprocess.TimeoutExpired as e:
        raise DirenvError("direnv export timed out") from e

    if result.returncode != 0:
        raise DirenvError(f"direnv export failed: {result.stderr}")

    # direnv export bash outputs shell commands like:
    # export VAR="value"; export VAR2="value2";
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        print("direnv: CLAUDE_ENV_FILE not available", file=sys.stderr)
        return

    # Write the exports to CLAUDE_ENV_FILE
    if result.stdout.strip():
        Path(env_file).write_text(result.stdout)
        # Print direnv-style export banner (summarize changes)
        exports = []
        for part in result.stdout.split("export "):
            if "=" in part:
                var = part.split("=")[0].strip()
                if var:
                    exports.append(f"+{var}")
        if exports:
            print(
                f"direnv: export {' '.join(exports[:5])}"
                + (f" ... (+{len(exports) - 5} more)" if len(exports) > 5 else "")
            )


# ============================================================================
# Web mode: Bazel proxy and environment setup
# ============================================================================


def get_nix_status() -> str:
    """Get status of nix installation."""
    nix_bin = nix_setup.find_nix_bin()
    if nix_bin:
        return f"installed ({nix_bin})"
    return "not installed"


def format_environment_summary() -> str:
    """Format a compact environment summary with deduplicated proxy values."""
    env = dict(os.environ)

    # Group env vars by their value to deduplicate long proxy URLs
    value_to_vars: dict[str, list[str]] = {}
    for key, value in sorted(env.items()):
        if value not in value_to_vars:
            value_to_vars[value] = []
        value_to_vars[value].append(key)

    lines = []

    # Find proxy-related values (long URLs that appear in multiple vars)
    proxy_vars = {}
    other_vars = {}

    for value, keys in value_to_vars.items():
        # Identify proxy values by checking if they're long URLs used by multiple vars
        is_proxy = len(value) > 100 and any(
            k for k in keys if "PROXY" in k.upper() or k in ("http_proxy", "https_proxy")
        )
        if is_proxy and len(keys) > 1:
            proxy_vars[value] = keys
        else:
            for key in keys:
                other_vars[key] = value

    # Output proxy values with their aliases
    if proxy_vars:
        lines.append("Proxy configuration:")
        for i, (value, keys) in enumerate(proxy_vars.items(), 1):
            # Truncate the URL for display
            truncated = value[:80] + "..." if len(value) > 80 else value
            lines.append(f"  proxy_{i}: {truncated}")
            lines.append(f"    Used by: {', '.join(sorted(keys))}")

    # Output key environment vars (not all, just important ones)
    important_keys = [
        "CLAUDE_CODE_REMOTE",
        "CLAUDE_CODE_VERSION",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_ENV_FILE",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "DOCKER_HOST",
        "PATH",
    ]

    lines.append("Key environment:")
    for key in important_keys:
        if key in other_vars:
            value = other_vars[key]
            # Truncate long values
            if len(value) > 100:
                value = value[:97] + "..."
            lines.append(f"  {key}={value}")

    return "\n".join(lines)


def emit_session_context(collector: LogCollector) -> None:
    """Emit compact context summary for Claude Code transcript.

    This goes to stdout and gets injected as context for the agent.
    Includes any warnings/errors that occurred during setup.
    """
    has_errors = len(collector.errors) > 0
    has_warnings = len(collector.warnings) > 0

    lines = [
        "Claude Code on the web (gVisor sandbox)",
        "Status: " + ("ERRORS" if has_errors else "OK with warnings" if has_warnings else "OK"),
        "Constraints:",
        "  - TLS-inspecting proxy (custom CA configured)",
        "  - No overlay filesystem (use vfs for containers)",
        "  - Network via proxy only (no direct DNS)",
    ]

    if collector.errors:
        lines.append("Errors:")
        lines.extend(f"  - {msg}" for msg in collector.errors)

    if collector.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {msg}" for msg in collector.warnings)

    lines.append(f"Full log: {LOG_FILE}")

    print("\n".join(lines))
    sys.stdout.flush()


def install_git_precommit_hook(project_dir: Path) -> None:
    """Install git pre-commit hook using pre-commit framework.

    First ensures pre-commit is installed via pip, then runs `pre-commit install`
    which installs the hook defined in .pre-commit-config.yaml.
    This includes conflict marker detection, syntax checks, and bazel lint.
    """
    git_dir = project_dir / ".git"
    if not git_dir.exists():
        logger.info("Not a git repository (no .git), skipping git hook install")
        return

    precommit_config = project_dir / ".pre-commit-config.yaml"
    if not precommit_config.exists():
        logger.warning("No .pre-commit-config.yaml found, skipping git hook install")
        return

    hook_target = git_dir / "hooks" / "pre-commit"
    if hook_target.exists():
        logger.info("Git pre-commit hook already installed")
        return

    # Ensure pre-commit is installed (version from .pre-commit-config.yaml comment)
    try:
        subprocess.run(["pre-commit", "--version"], capture_output=True, check=True, timeout=5)
        logger.info("pre-commit already available")
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.info("Installing pre-commit==4.0.1 via pip")
        try:
            result = subprocess.run(
                ["pip", "install", "--user", "pre-commit==4.0.1"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning("Failed to install pre-commit: %s", result.stderr)
                return
            logger.info("pre-commit installed successfully")
        except subprocess.TimeoutExpired:
            logger.warning("pre-commit installation timed out")
            return

    # Install the git hook
    try:
        result = subprocess.run(
            ["pre-commit", "install"], check=False, cwd=project_dir, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info("Installed git pre-commit hook via pre-commit install")
        else:
            logger.warning("pre-commit install failed: %s", result.stderr)
    except FileNotFoundError:
        logger.warning("pre-commit not found after installation attempt")
    except subprocess.TimeoutExpired:
        logger.warning("pre-commit install timed out")


class LogCollector(logging.handlers.MemoryHandler):
    """Handler that collects log records for later inspection.

    Uses MemoryHandler with high capacity and no auto-flush to buffer all records.
    """

    def __init__(self) -> None:
        # Large capacity, no flush level, no target - just collect
        super().__init__(capacity=1000, flushLevel=logging.CRITICAL + 1)

    @property
    def warnings(self) -> list[str]:
        return [self.format(r) for r in self.buffer if r.levelno == logging.WARNING]

    @property
    def errors(self) -> list[str]:
        return [self.format(r) for r in self.buffer if r.levelno >= logging.ERROR]


def setup_podman_storage() -> None:
    """Configure podman for gVisor compatibility.

    gVisor sandbox has restrictions that require specific podman configuration:
    1. VFS storage driver (no overlay filesystem support)
    2. System-level config (/etc/containers) since running as root
    3. Explicit runroot and graphroot paths
    4. Host user namespace (userns = "host")
    """
    podman_config: Traversable = importlib.resources.files("claude_hooks.config.podman")

    # Storage configuration (system-level since running as root)
    storage_conf = Path("/etc/containers/storage.conf")
    storage_conf.parent.mkdir(parents=True, exist_ok=True)
    storage_conf.write_text(podman_config.joinpath("storage.conf").read_text())

    # Container runtime configuration
    containers_conf = Path("/etc/containers/containers.conf")
    containers_conf.write_text(podman_config.joinpath("containers.conf").read_text())

    # Ensure storage directories exist
    Path("/run/containers/storage").mkdir(parents=True, exist_ok=True)
    Path("/var/lib/containers/storage").mkdir(parents=True, exist_ok=True)

    logger.info("Configured podman for gVisor: VFS storage, host userns")


def emit_podman_guidance() -> None:
    """Emit podman usage guidance for gVisor sandbox (visible to agent)."""
    guidance = textwrap.dedent(
        """\
        Podman in gVisor Sandbox
        ========================
        Podman is configured with gVisor-specific workarounds.

        Required Container Flags
        ------------------------
        All containers MUST use `--annotation run.oci.keep_original_groups=1`.
        This bypasses /proc/self/setgroups which is unavailable in gVisor.
        Otherwise they will fail with:
          "crun: error opening file `/proc/self/setgroups`: No such file or directory"

        Use fully qualified image names (docker.io/library/...)

        Configuration Applied:
        ----------------------
        - VFS storage (/etc/containers/storage.conf)
        - userns = "host"
        - --network=host
        """
    )
    print(guidance)
    sys.stdout.flush()


def setup_logging() -> LogCollector:
    """Configure module logger with stdout, file, and collector handlers.

    Returns LogCollector for use in emit_session_context.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    collector = LogCollector()
    collector.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.propagate = False

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.addHandler(collector)

    return collector


def run_web_mode(hook_input: HookInput) -> None:
    """Web mode: set up Bazel proxy, git hooks, and development environment."""
    collector = setup_logging()

    logger.info("Session start hook")
    logger.info("Hook: %s", __file__)
    logger.info("Log:  %s", LOG_FILE)
    logger.info("Hook input: %s", hook_input.model_dump_json())

    logger.info("Full environment:\n%s", json.dumps(dict(os.environ), sort_keys=True, indent=2))
    logger.info("Setting up dev environment...")
    logger.info(format_environment_summary())

    # Detect project directory
    project_dir_str = os.environ.get("CLAUDE_PROJECT_DIR")
    project_dir: Path
    if project_dir_str:
        logger.info("CLAUDE_PROJECT_DIR provided: %s", project_dir_str)
        project_dir = Path(project_dir_str)
    else:
        logger.warning("CLAUDE_PROJECT_DIR not provided (fallback to PWD)")
        pwd = Path.cwd()
        if (pwd / ".git").exists():
            project_dir = pwd
            os.environ["CLAUDE_PROJECT_DIR"] = str(project_dir)
            logger.info("Project: %s", project_dir)
        else:
            raise ProjectNotFoundError("Cannot detect project root (no .git, CLAUDE_PROJECT_DIR not set)")

    # Install Bazelisk
    bazelisk_setup.install_bazelisk()

    # Set up Bazel proxy
    proxy_setup.setup_bazel_proxy()

    # Install bazel wrapper
    bazelisk_setup.install_wrapper()

    install_git_precommit_hook(project_dir)

    # Install cluster tools for pre-commit hooks (cluster/ always exists in ducktape)
    logger.info("Installing cluster tools for pre-commit hooks...")
    binary_tools.install_cluster_tools()

    # Install dev tools (alejandra for .nix formatting)
    logger.info("Installing dev tools (alejandra)...")
    binary_tools.install_dev_tools()

    # Install nix (for nix eval, etc. - alejandra is now installed via binary download)
    logger.info("Installing nix...")
    nix_store_bin: Path | None = None
    try:
        nix_store_bin = nix_setup.install_nix()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Failed to install nix: %s", e)

    # Configure podman for gVisor compatibility
    logger.info("Configuring podman for e2e testing...")
    setup_podman_storage()
    emit_podman_guidance()

    # Export debug timestamp
    hook_timestamp = datetime.now()
    hook_timestamp_str = hook_timestamp.isoformat()
    os.environ["DUCKTAPE_SESSION_START_HOOK_TS"] = hook_timestamp_str
    TIMESTAMP_FILE.write_text(f"{hook_timestamp_str}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp_str)

    # Configure PATH for bash sessions
    logger.info("Configuring bazel availability for bash sessions...")
    env_file_str = os.environ.get("CLAUDE_ENV_FILE")
    if env_file_str:
        env_path = Path(env_file_str)

        # Generate consolidated env script with all settings
        # Combined CA bundle must exist at this point (created by setup_bazel_proxy)
        combined_ca = proxy_setup._get_bazel_combined_ca()
        if not combined_ca.exists():
            raise RuntimeError("Combined CA bundle not found - proxy setup incomplete")
        nix_paths = nix_setup.get_nix_paths(nix_store_bin) if nix_store_bin else []

        env_content = bazelisk_setup.get_env_script(
            proxy_port=proxy_setup._get_bazel_proxy_port(),
            repo_root=project_dir,
            hook_timestamp=hook_timestamp,
            combined_ca=combined_ca,
            nix_paths=nix_paths,
        )

        env_path.write_text(env_content)
        logger.info("Wrote PATH exports to %s", env_path)
    else:
        # Fallback: symlink bazel to ~/.local/bin
        logger.warning("CLAUDE_ENV_FILE not provided, using symlink fallback")
        local_bin = Path.home() / ".local" / "bin"
        current_path = os.environ.get("PATH", "")
        if str(local_bin) not in current_path:
            logger.warning("~/.local/bin not in PATH - bazel may not be available")

        local_bin.mkdir(parents=True, exist_ok=True)
        bazel_symlink = local_bin / "bazel"
        bazel_wrapper = bazelisk_setup._get_wrapper_path()

        if bazel_symlink.exists() or bazel_symlink.is_symlink():
            if bazel_symlink.is_symlink() and bazel_symlink.resolve() == bazel_wrapper.resolve():
                logger.info("Bazel symlink already configured")
            else:
                logger.warning("Replacing existing bazel with symlink")
                bazel_symlink.unlink()
                bazel_symlink.symlink_to(bazel_wrapper)
        else:
            bazel_symlink.symlink_to(bazel_wrapper)
            logger.info("Created bazel symlink: %s -> %s", bazel_symlink, bazel_wrapper)

    # Compact summary for stdout
    node_ca_status = "custom CA" if proxy_setup._get_bazel_combined_ca().exists() else "system"
    logger.info(
        "Ready: bazel=%s, proxy=%s, CA=%s", bazelisk_setup.get_status(), proxy_setup.get_status(), node_ca_status
    )
    logger.info("Cluster tools: %s", binary_tools.get_cluster_tools_status())
    logger.info("Dev tools: %s", binary_tools.get_dev_tools_status())
    logger.info("Nix: %s", get_nix_status())

    supervisor_setup.emit_usage_guidance()

    # Emit context for Claude Code
    emit_session_context(collector)


def main() -> None:
    """Unified entry point: dispatch to web or CLI mode based on environment."""
    hook_input = HookInput.model_validate_json(sys.stdin.read())

    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        run_web_mode(hook_input)
    else:
        run_cli_mode(hook_input)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Can't rely on log here since setup may have failed
        print(f"Hook failed: {e}", file=sys.stderr)
        print(f"Hook: {__file__}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
