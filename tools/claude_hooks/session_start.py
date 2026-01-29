"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy and git hooks.
CLI mode: Loads direnv environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import shlex
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from fmt_util.fmt_util import format_limited_list
from tools import env_utils
from tools.build_info import BUILD_COMMIT
from tools.claude_hooks import bazelisk_setup, env_file, nix_setup, podman_service, proxy_setup
from tools.claude_hooks.debug import log_entrypoint_debug
from tools.claude_hooks.errors import DirenvError, SkipError
from tools.claude_hooks.settings import HookSettings
from tools.claude_hooks.supervisor import setup as supervisor_setup

logger = logging.getLogger(__name__)


class HookSource(StrEnum):
    """Source of the SessionStart hook event."""

    STARTUP = "startup"
    RESUME = "resume"
    CLEAR = "clear"
    COMPACT = "compact"


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
    source: HookSource


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

    # Use direnv to export the environment as JSON
    try:
        result = await asyncio.create_subprocess_exec(
            "direnv", "export", "json", cwd=envrc.parent, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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

    env_file_path = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file_path:
        print("direnv: CLAUDE_ENV_FILE not available", file=sys.stderr)
        return

    if stdout_str.strip():
        env_vars: dict[str, str] = json.loads(stdout_str)
        # Write as shell export statements for CLAUDE_ENV_FILE
        lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(env_vars.items())]
        Path(env_file_path).write_text("\n".join(lines) + "\n")
        # Print direnv-style export banner (summarize changes)
        exports = [f"+{key}" for key in sorted(env_vars)]
        if exports:
            print(f"direnv: export {format_limited_list(exports, 5, separator=' ')}")


# ============================================================================
# Web mode: Auth proxy and environment setup
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


def emit_session_context(collector: LogCollector, log_file: Path) -> None:
    """Emit compact context summary for Claude Code transcript.

    This goes to stdout and gets injected as context for the agent.
    Includes any warnings/errors that occurred during setup.
    """
    has_errors = len(collector.errors) > 0
    has_warnings = len(collector.warnings) > 0

    lines = [
        f"Claude Code on the web (gVisor sandbox) [build: {BUILD_COMMIT}]",
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
        lines.append(
            "  Use via: curl -H 'Authorization: Bearer $DUCKTAPE_CI_READ_GITHUB_TOKEN' https://api.github.com/..."
        )
        lines.append("  Capabilities: read repo, read CI logs, list workflow runs, view PR status.")

    lines.append(f"Full log: {log_file}")

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

    # Ensure pre-commit and its dependencies are installed
    # ansible-lint hook requires ansible package
    # TODO: Deduplicate this setup with CI setup steps (see .github/workflows/*.yml)
    try:
        subprocess.run(["pre-commit", "--version"], capture_output=True, check=True, timeout=5)
        logger.info("pre-commit already available")
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.info("Installing pre-commit==4.0.1 and ansible via pip")
        try:
            result = subprocess.run(
                ["pip", "install", "--user", "pre-commit==4.0.1", "ansible"],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
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


def setup_logging(settings: HookSettings) -> LogCollector:
    """Configure root logger so all modules in tools.claude_hooks get handlers.

    Returns LogCollector for use in emit_session_context.
    """
    log_file = settings.get_cache_dir() / "session-start.log"

    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    collector = LogCollector()
    collector.setFormatter(formatter)

    # Configure root logger so all child loggers (proxy_setup, bazelisk_setup, etc.) inherit
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    root_logger.addHandler(collector)

    # Attach log_file to collector so callers can access it
    collector.log_file = log_file  # type: ignore[attr-defined]

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


async def run_web_mode(hook_input: HookInput, settings: HookSettings) -> None:
    """Web mode with parallelized operations.

    Uses asyncio to parallelize independent installations (git hook, cluster
    tools, nix) while maintaining correct sequencing for dependent operations.

    Writes CLAUDE_ENV_FILE once at the end with all collected environment
    variables.
    """
    collector = setup_logging(settings)
    log_file = collector.log_file  # type: ignore[attr-defined]

    logger.info("Session start hook")
    logger.info("Hook: %s", __file__)
    logger.info("Log:  %s", log_file)
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")
    logger.info("Setting up dev environment...")
    logger.info(format_environment_summary())

    # Get required environment variables (fail early if missing)
    env_file_path = env_utils.get_required_env_path("CLAUDE_ENV_FILE")

    # Get required project directory
    project_dir = env_utils.get_required_env_path("CLAUDE_PROJECT_DIR")
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)

    # Start supervisor (required by proxy and podman)
    supervisor_task = asyncio.create_task(supervisor_setup.start(settings))

    # Wrappers that depend on supervisor being ready
    # TODO: Handle upstream dependency failures more gracefully.
    # Currently, when supervisor_task fails, all downstream tasks (proxy, podman)
    # re-raise the same exception, resulting in N copies of the upstream error.
    # Consider: skip downstream tasks silently or return a sentinel value instead
    # of re-raising, so only the original upstream error surfaces once.
    async def setup_proxy_with_supervisor() -> proxy_setup.ProxySetup:
        """Set up auth proxy (depends on supervisor)."""
        supervisor_result = await supervisor_task
        return await proxy_setup.setup_auth_proxy(settings, supervisor_result.client)

    async def setup_podman_with_supervisor() -> podman_service.PodmanSetup:
        """Set up podman (depends on supervisor)."""
        supervisor_result = await supervisor_task
        return await podman_service.setup_podman(settings, supervisor_result.client)

    def install_bazelisk_wrapper() -> bazelisk_setup.BazeliskSetup:
        """Install bazelisk and wrapper as separate tasks.

        Always installs the wrapper. Optionally downloads bazelisk unless
        DUCKTAPE_CLAUDE_HOOKS_SKIP_BAZELISK is set.
        """
        wrapper_path = bazelisk_setup.install_wrapper(settings)
        skipped = settings.skip_bazelisk
        if not skipped:
            bazelisk_setup.install_bazelisk(settings)
        else:
            logger.info("Skipping bazelisk download (skip_bazelisk=True)")
        return bazelisk_setup.BazeliskSetup(
            bazelisk_path=settings.get_bazelisk_path(),
            wrapper_path=wrapper_path,
            settings=settings,
            bazelisk_skipped=skipped,
        )

    def setup_buildbuddy() -> str | None:
        """Configure BuildBuddy remote cache if BUILDBUDDY_API_KEY is set.

        Writes config to ~/.config/bazel/buildbuddy.bazelrc and ensures
        ~/.bazelrc has the try-import line.
        """
        api_key = os.environ.get("BUILDBUDDY_API_KEY")
        if not api_key:
            logger.info("BUILDBUDDY_API_KEY not set, skipping BuildBuddy setup")
            return None

        # Run the setup script
        script_path = project_dir / "tools" / "setup-buildbuddy.sh"
        if not script_path.exists():
            logger.warning("BuildBuddy setup script not found: %s", script_path)
            return None

        result = subprocess.run(
            [script_path],
            env={**os.environ, "BUILDBUDDY_API_KEY": api_key},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            logger.info("BuildBuddy remote cache configured")
            return "configured"
        logger.warning("BuildBuddy setup failed: %s", result.stderr)
        return None

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    logger.info("Starting parallel installations...")
    results = await asyncio.gather(
        setup_proxy_with_supervisor(),
        setup_podman_with_supervisor(),
        run_in_thread(install_git_precommit_hook, project_dir),
        run_in_thread(nix_setup.install_nix, settings),
        run_in_thread(install_bazelisk_wrapper),
        run_in_thread(setup_buildbuddy),
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    podman_result: podman_service.PodmanSetup | BaseException = results[1]
    git_result: None | BaseException = results[2]
    nix_result: Path | None | BaseException = results[3]
    bazelisk_result: bazelisk_setup.BazeliskSetup | BaseException = results[4]
    buildbuddy_result: str | None | BaseException = results[5]

    # Log non-critical failures (git, bazelisk, nix, podman, buildbuddy)
    if isinstance(git_result, BaseException):
        logger.warning("Failed to install git pre-commit: %s", git_result)
    if isinstance(bazelisk_result, BaseException):
        logger.warning("Failed to install bazelisk: %s", bazelisk_result)
    if isinstance(buildbuddy_result, BaseException):
        logger.warning("Failed to configure BuildBuddy: %s", buildbuddy_result)

    # Extract artifacts
    nix_store_bin: Path | None = None if isinstance(nix_result, BaseException) else nix_result
    if isinstance(nix_result, SkipError):
        logger.info("Nix setup skipped: %s", nix_result)
    elif isinstance(nix_result, BaseException):
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
    timestamp_file = settings.get_cache_dir() / "session-hook-last-run"
    timestamp_file.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Proxy setup is required - propagate failure with clear error message
    if isinstance(proxy_result, BaseException):
        logger.error("Proxy setup failed: %s", proxy_result)
        raise RuntimeError(f"Proxy setup failed: {proxy_result}") from proxy_result
    # At this point, proxy_result is ProxySetup (type narrowed by the check above)

    # Verify combined CA was created (sanity check - should always exist after successful proxy setup)
    combined_ca = settings.get_auth_proxy_combined_ca()
    if not combined_ca.exists():
        raise RuntimeError("Combined CA bundle not found - proxy setup incomplete")

    nix_paths = nix_setup.get_nix_paths(nix_store_bin) if nix_store_bin else []

    # Determine bazelisk_path: use system_bazel if skip_bazelisk, otherwise downloaded bazelisk
    if isinstance(bazelisk_result, bazelisk_setup.BazeliskSetup) and bazelisk_result.bazelisk_skipped:
        if settings.system_bazel is not None:
            bazelisk_path = settings.system_bazel
        else:
            # Auto-detect system bazelisk/bazel
            auto_bazel = shutil.which("bazelisk") or shutil.which("bazel")
            if not auto_bazel:
                raise RuntimeError("skip_bazelisk=True but no bazelisk/bazel found on PATH")
            bazelisk_path = Path(auto_bazel)
    else:
        bazelisk_path = settings.get_bazelisk_path()

    env_vars = env_file.EnvVars(
        proxy_port=settings.get_auth_proxy_port(),
        supervisor_port=settings.get_supervisor_port(),
        repo_root=project_dir,
        combined_ca=combined_ca,
        bazel_wrapper_dir=settings.get_wrapper_dir(),
        bazelisk_path=bazelisk_path,
        auth_proxy_rc=settings.get_auth_proxy_rc(),
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
        logger.info("Podman: %s", podman_result.status)

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

    emit_session_context(collector, log_file)


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
        settings = HookSettings()
        await run_web_mode(hook_input, settings)
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
