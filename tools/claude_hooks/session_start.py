"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up Bazel proxy and git hooks.
CLI mode: Loads direnv environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from tools import env_utils
from tools.claude_hooks import (
    bazelisk_setup,
    binary_tools,
    env_file,
    nix_setup,
    paths,
    podman_service,
    proxy_setup,
    supervisor_setup,
)
from tools.claude_hooks.errors import DirenvError, SkipError

LOG_FILE = paths.get_cache_dir() / "session-start.log"
TIMESTAMP_FILE = paths.get_cache_dir() / "session-hook-last-run"

logger = logging.getLogger(__name__)


class HookInput(BaseModel):
    """Input passed to Claude Code hooks via stdin.

    Note: permission_mode is optional because Claude Code Web was observed
    (2025-01-18) not sending it for SessionStart:resume events, despite
    documentation claiming it's required.
    """

    session_id: str
    cwd: Path
    transcript_path: str
    permission_mode: Literal["default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"] = "default"
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


async def run_cli_mode(hook_input: HookInput) -> None:
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
        result = await asyncio.create_subprocess_exec(
            "direnv", "export", "bash", cwd=envrc.parent, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=30)
    except FileNotFoundError:
        print("direnv: not installed, skipping", file=sys.stderr)
        return
    except TimeoutError as e:
        raise DirenvError("direnv export timed out") from e

    if result.returncode != 0:
        raise DirenvError(f"direnv export failed: {stderr.decode()}")

    stdout_str = stdout.decode()

    # direnv export bash outputs shell commands like:
    # export VAR="value"; export VAR2="value2";
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        print("direnv: CLAUDE_ENV_FILE not available", file=sys.stderr)
        return

    # Write the exports to CLAUDE_ENV_FILE
    if stdout_str.strip():
        Path(env_file).write_text(stdout_str)
        # Print direnv-style export banner (summarize changes)
        exports = []
        for part in stdout_str.split("export "):
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
        "  - 9p filesystem (no hard links on Unix sockets)",
    ]

    if collector.errors:
        lines.append("Errors:")
        lines.extend(f"  - {msg}" for msg in collector.errors)

    if collector.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {msg}" for msg in collector.warnings)

    # Check for GitHub CI token
    if os.environ.get("DUCKTAPE_CI_READ_GITHUB_TOKEN"):
        lines.append("GitHub CI Access:")
        lines.append("  DUCKTAPE_CI_READ_GITHUB_TOKEN is set - GitHub PAT with read access to ducktape repo.")
        lines.append("  Use with `gh` CLI: GH_TOKEN=$DUCKTAPE_CI_READ_GITHUB_TOKEN gh ...")
        lines.append("  Capabilities: read repo, read CI logs, list workflow runs, view PR status.")
        lines.append("  Workflow: push to branch, ask user to create PR, then poll CI status via gh.")

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


def setup_logging() -> LogCollector:
    """Configure root logger so all modules in tools.claude_hooks get handlers.

    Returns LogCollector for use in emit_session_context.
    """
    paths.get_cache_dir().mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    collector = LogCollector()
    collector.setFormatter(formatter)

    # Configure root logger so all child loggers (proxy_setup, bazelisk_setup, etc.) inherit
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    root_logger.addHandler(collector)

    return collector


# ============================================================================
# Async helpers
# ============================================================================


async def run_in_thread(func, *args):
    """Run blocking function in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


# ============================================================================
# Web mode: async setup with parallelization
# ============================================================================


async def run_web_mode(hook_input: HookInput) -> None:
    """Web mode with parallelized operations.

    Uses asyncio to parallelize independent installations (git hook, cluster
    tools, nix) while maintaining correct sequencing for dependent operations.

    Writes CLAUDE_ENV_FILE once at the end with all collected environment
    variables.
    """
    collector = setup_logging()

    logger.info("Session start hook")
    logger.info("Hook: %s", __file__)
    logger.info("Log:  %s", LOG_FILE)
    logger.info("Hook input: %s", hook_input.model_dump_json())
    logger.info("Full environment:\n%s", json.dumps(dict(os.environ), sort_keys=True, indent=2))
    logger.info("Setting up dev environment...")
    logger.info(format_environment_summary())

    # Get required environment variables (fail early if missing)
    env_file_path = env_utils.get_required_env_path("CLAUDE_ENV_FILE")

    # Get required project directory
    project_dir = env_utils.get_required_env_path("CLAUDE_PROJECT_DIR")
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)

    # Start supervisor (required by proxy and podman)
    supervisor_task = asyncio.create_task(run_in_thread(supervisor_setup.start))

    # Wrappers that depend on supervisor being ready
    # TODO: Handle upstream dependency failures more gracefully.
    # Currently, when supervisor_task fails, all downstream tasks (proxy, podman)
    # re-raise the same exception, resulting in N copies of the upstream error.
    # Consider: skip downstream tasks silently or return a sentinel value instead
    # of re-raising, so only the original upstream error surfaces once.
    async def setup_proxy_with_supervisor() -> proxy_setup.ProxySetup:
        """Set up bazel proxy (depends on supervisor)."""
        await supervisor_task
        if exc := supervisor_task.exception():
            raise exc
        supervisor_result = supervisor_task.result()
        return proxy_setup.setup_bazel_proxy(supervisor_result.client)

    async def setup_podman_with_supervisor() -> podman_service.PodmanSetup:
        """Set up podman (depends on supervisor)."""
        await supervisor_task
        if exc := supervisor_task.exception():
            raise exc
        supervisor_result = supervisor_task.result()
        return podman_service.setup_podman(supervisor_result.client)

    def install_bazelisk_wrapper() -> bazelisk_setup.BazeliskSetup:
        """Install bazelisk and wrapper."""
        if os.environ.get("CLAUDE_HOOKS_SKIP_BAZELISK"):
            logger.info("Skipping bazelisk installation (CLAUDE_HOOKS_SKIP_BAZELISK set)")
            raise SkipError("Bazelisk", "CLAUDE_HOOKS_SKIP_BAZELISK")
        return bazelisk_setup.install_bazelisk_and_wrapper()

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    logger.info("Starting parallel installations...")
    results = await asyncio.gather(
        setup_proxy_with_supervisor(),
        setup_podman_with_supervisor(),
        run_in_thread(install_git_precommit_hook, project_dir),
        run_in_thread(binary_tools.install_cluster_tools),
        run_in_thread(nix_setup.install_nix),
        run_in_thread(install_bazelisk_wrapper),
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    podman_result: podman_service.PodmanSetup | BaseException = results[1]
    git_result: None | BaseException = results[2]
    cluster_result: None | BaseException = results[3]
    nix_result: Path | None | BaseException = results[4]
    bazelisk_result: bazelisk_setup.BazeliskSetup | BaseException = results[5]

    # Log non-critical failures (git, cluster tools, bazelisk, nix, podman)
    if isinstance(git_result, BaseException):
        logger.warning("Failed to install git pre-commit: %s", git_result)
    if isinstance(cluster_result, BaseException):
        logger.warning("Failed to install cluster tools: %s", cluster_result)
    if isinstance(bazelisk_result, BaseException):
        logger.warning("Failed to install bazelisk: %s", bazelisk_result)

    # Extract artifacts
    nix_store_bin: Path | None = None if isinstance(nix_result, BaseException) else nix_result
    if isinstance(nix_result, BaseException):
        logger.warning("Failed to install nix: %s", nix_result)

    docker_host: str | None = None
    podman_env: dict[str, str] | None = None
    if isinstance(podman_result, SkipError):
        logger.info("Podman setup skipped: %s", podman_result)
    elif isinstance(podman_result, BaseException):
        logger.warning("Failed to configure podman: %s", podman_result)
    else:
        docker_host = podman_result.socket_url
        podman_env = podman_result.env_vars

    # Generate timestamp
    hook_timestamp = datetime.now()
    TIMESTAMP_FILE.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Proxy setup is required - propagate failure with clear error message
    if isinstance(proxy_result, BaseException):
        logger.error("Proxy setup failed: %s", proxy_result)
        raise RuntimeError(f"Proxy setup failed: {proxy_result}") from proxy_result
    # At this point, proxy_result is ProxySetup (type narrowed by the check above)

    # Verify combined CA was created (sanity check - should always exist after successful proxy setup)
    combined_ca = proxy_setup._get_bazel_combined_ca()
    if not combined_ca.exists():
        raise RuntimeError("Combined CA bundle not found - proxy setup incomplete")

    nix_paths = nix_setup.get_nix_paths(nix_store_bin) if nix_store_bin else []

    env_vars = env_file.EnvVars(
        proxy_port=proxy_setup._get_bazel_proxy_port(),
        repo_root=project_dir,
        combined_ca=combined_ca,
        bazel_wrapper_dir=bazelisk_setup._get_wrapper_path().parent,
        bazelisk_path=bazelisk_setup._get_bazelisk_path(),
        bazel_proxy_rc=proxy_setup._get_bazel_proxy_rc(),
        nix_paths=nix_paths,
        docker_host=docker_host,
        podman_env=podman_env,
        hook_timestamp=hook_timestamp,
    )

    # Write environment file ONCE
    env_file.write_env_file(env_file_path, env_vars)
    logger.info("Wrote environment to %s", env_file_path)

    # Emit status
    if isinstance(bazelisk_result, SkipError):
        bazel_status = "skipped"
    elif isinstance(bazelisk_result, BaseException):
        bazel_status = "not installed"
    else:
        bazel_status = bazelisk_result.status
    # proxy_result is already narrowed to ProxySetup after the check above
    proxy_status = proxy_result.status
    ca_status = proxy_result.ca_status
    logger.info("Ready: bazel=%s, proxy=%s, CA=%s", bazel_status, proxy_status, ca_status)
    logger.info("Nix: %s", get_nix_status())
    if not isinstance(podman_result, BaseException):
        podman_status = (
            "running" if podman_result.supervisor.is_service_running("podman", wait_for_start=False) else "not running"
        )
        logger.info("Podman: %s", podman_status)

    # Emit all collected guidance
    if not isinstance(supervisor_task.result(), BaseException):
        print(supervisor_task.result().guidance)
        sys.stdout.flush()
    # proxy_result is already narrowed to ProxySetup
    proxy_guidance = proxy_result.guidance
    if proxy_guidance:
        print(proxy_guidance)
        sys.stdout.flush()
    if not isinstance(podman_result, BaseException):
        print(podman_result.guidance)
        sys.stdout.flush()

    emit_session_context(collector)


async def async_main() -> None:
    """Async entry point: dispatch to web or CLI mode based on environment."""
    raw_input = sys.stdin.read()
    try:
        hook_input = HookInput.model_validate_json(raw_input)
    except Exception as e:
        print(f"Failed to parse hook input: {e}", file=sys.stderr)
        print(f"Raw input JSON:\n{raw_input}", file=sys.stderr)
        raise

    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        await run_web_mode(hook_input)
    else:
        await run_cli_mode(hook_input)


def main() -> None:
    """Synchronous entry point for console_scripts."""
    try:
        asyncio.run(async_main())
    except Exception as e:
        # Can't rely on log here since setup may have failed
        print(f"Hook failed: {e}", file=sys.stderr)
        print(f"Hook: {__file__}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
