"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy and git hooks.
CLI mode: Sets up per-session bazel wrapper with direnv integration.

Both modes render a per-session bazelrc from the unified bazelrc.mako template
and install a bazel wrapper that injects --bazelrc=<session-bazelrc>.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
import traceback
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from mako.template import Template
from pydantic import BaseModel

from bazel_util.subprocess import write_shell_wrapper
from env_utils import env_utils
from tools.build_info import BUILD_COMMIT
from tools.claude_hooks import (
    bazelisk_setup,
    buildbuddy_setup,
    cli_tools_setup,
    env_file,
    kubeconfig_setup,
    mkcert_setup,
    nix_setup,
    podman_service,
    precommit_setup,
    proxy_setup,
    secrets_setup,
    tmpfs_setup,
)
from tools.claude_hooks.debug import log_entrypoint_debug
from tools.claude_hooks.errors import SkipError
from tools.claude_hooks.managed_files import write_config
from tools.claude_hooks.settings import CONFIG_FILES, HookSettings
from tools.claude_hooks.supervisor import setup as supervisor_setup

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Per-repo config directory, resolved from CLAUDE_PROJECT_DIR at runtime.
# Secrets and repo-specific templates live here, NOT in the wheel.
_HOOKS_DOTDIR = ".claude_hooks"


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
# Shared helpers (used by both web and CLI modes)
# ============================================================================


def _render_session_bazelrc(session_dir: Path, **template_vars: object) -> Path:
    """Render bazelrc.mako template to session_dir/bazelrc.

    Both modes use the same template with different variables:
    - CLI: web_proxy=False
    - Web: web_proxy=True, proxy_port=..., etc.
    """
    template = Template(CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"])
    result: str = template.render(**template_vars)
    bazelrc_path = session_dir / "bazelrc"
    write_config(bazelrc_path, result, "session bazelrc")
    return bazelrc_path


def _get_session_bin_dir(session_dir: Path) -> Path:
    """Create and return the session-scoped bin directory for tools and wrappers."""
    bin_dir = session_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return bin_dir


def _install_bazel_wrappers(bin_dir: Path) -> None:
    """Install bazel/bazelisk wrapper scripts into the session bin directory.

    Creates bin/bazel (shell wrapper → bazel_wrapper.py) and bin/bazelisk
    (symlink → bin/bazel). The shell wrapper exports _BAZEL_WRAPPER_NAME
    and _BAZEL_WRAPPER_DIR so the Python module can route to the correct
    binary and skip its own directory when searching PATH.
    """

    extra_lines = (
        'export _BAZEL_WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd)"\nexport _BAZEL_WRAPPER_NAME="$(basename "$0")"'
    )

    bazel_path = bin_dir / "bazel"
    write_shell_wrapper(bazel_path, "tools.claude_hooks.bazel_wrapper", extra_lines=extra_lines)

    # Create bazelisk symlink so both names route through the wrapper
    bazelisk_path = bin_dir / "bazelisk"
    if bazelisk_path.exists() or bazelisk_path.is_symlink():
        bazelisk_path.unlink()
    bazelisk_path.symlink_to(bazel_path)

    logger.info("Installed bazel wrappers in %s", bin_dir)


# ============================================================================
# CLI mode: per-session bazel wrapper with direnv integration
# ============================================================================


async def run_cli_mode(hook_input: HookInput, settings: HookSettings) -> None:
    """CLI mode: set up per-session bazel wrapper and direnv eval.

    Renders a per-session bazelrc (with --config=ai_agent for quiet output),
    installs a bazel/bazelisk wrapper, and writes CLAUDE_ENV_FILE with the
    wrapper on PATH and direnv eval for .envrc propagation.
    """
    _collector, log_file = setup_logging(settings, print_banner=False)

    logger.info("Session start hook (CLI mode)")
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")

    env_file_path = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file_path:
        logger.warning("CLAUDE_ENV_FILE not available")
        return

    session_dir = Path(env_file_path).parent

    # Render per-session bazelrc (quiet mode)
    session_bazelrc = _render_session_bazelrc(session_dir, web_proxy=False)

    # Set up session bin dir with bazel wrappers
    bin_dir = _get_session_bin_dir(session_dir)
    _install_bazel_wrappers(bin_dir)

    # Write env file: puts bin dir on PATH, sets SESSION_BAZELRC, evals direnv
    env_file.write_cli_env_file(Path(env_file_path), wrapper_dir=bin_dir, session_bazelrc=session_bazelrc)

    logger.info("CLI session configured: %s", session_dir)
    print(f"Claude Code CLI session configured (log: {log_file})")


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


def _render_extra_context(project_dir: Path, secrets: secrets_setup.SecretsSetup | None) -> str:
    """Render repo-specific context from .claude_hooks/templates/context.mako if it exists."""
    extra_template_path = project_dir / _HOOKS_DOTDIR / "templates" / "context.mako"
    if not extra_template_path.exists():
        return ""
    template = Template(extra_template_path.read_text())
    result: str = template.render(secrets=secrets)
    return result.rstrip("\n")


def emit_session_context(
    collector: LogCollector,
    log_file: Path,
    project_dir: Path,
    auth_proxy: proxy_setup.ProxySetup,
    podman: podman_service.PodmanSetup | None,
    precommit: precommit_setup.PrecommitSetup | None,
    secrets: secrets_setup.SecretsSetup | None,
    mkcert: mkcert_setup.MkcertSetup | None = None,
) -> None:
    """Emit compact context summary for Claude Code transcript.

    Renders the generic session_context.mako (from the wheel) with structured
    setup results, then appends repo-specific context from
    .claude_hooks/templates/context.mako if it exists.
    """
    status = "ERRORS" if collector.has_errors else "OK with warnings" if collector.has_warnings else "OK"

    extra_context = _render_extra_context(project_dir, secrets)

    template = Template((_TEMPLATES_DIR / "session_context.mako").read_text())
    result: str = template.render(
        WARNING=logging.WARNING,
        build_commit=BUILD_COMMIT,
        status=status,
        proxy=auth_proxy,
        podman=podman,
        precommit=precommit,
        PrecommitInstallingHooks=precommit_setup.PrecommitInstallingHooks,
        mkcert=mkcert,
        log_entries=collector.buffer,
        secrets=secrets,
        extra_context=extra_context,
        log_file=log_file,
    )
    print(result.rstrip("\n"))
    sys.stdout.flush()


class LogCollector(logging.handlers.MemoryHandler):
    """Handler that collects log records for later inspection.

    Uses MemoryHandler with high capacity and no auto-flush to buffer all records.
    """

    def __init__(self) -> None:
        # Large capacity, no flush level, no target - just collect
        super().__init__(capacity=1000, flushLevel=logging.CRITICAL + 1)

    @property
    def has_errors(self) -> bool:
        return any(r.levelno >= logging.ERROR for r in self.buffer)

    @property
    def has_warnings(self) -> bool:
        return any(r.levelno == logging.WARNING for r in self.buffer)


def setup_logging(settings: HookSettings, *, print_banner: bool = True) -> tuple[LogCollector, Path]:
    """Configure root logger so all modules in tools.claude_hooks get handlers.

    Returns (LogCollector, log_file_path) tuple.
    """
    log_file = settings.get_cache_dir() / "session-start.log"

    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    collector = LogCollector()
    collector.setFormatter(formatter)

    # Configure root logger so all child loggers (proxy_setup, bazelisk_setup, etc.) inherit.
    # Logs go to file only — stdout is reserved for structured agent context.
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    root_logger.addHandler(collector)

    if print_banner:
        print(f"Setup log: {log_file}", file=sys.stderr)

    return collector, log_file


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
    collector, log_file = setup_logging(settings)

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

    # Mount tmpfs (required by podman overlay and Bazel cache)
    tmpfs_mount_task = asyncio.create_task(run_in_thread(tmpfs_setup.ensure_tmpfs_mounted))

    # Wrappers that depend on supervisor being ready
    # TODO: Handle upstream dependency failures more gracefully.
    # Currently, when supervisor_task fails, all downstream tasks (proxy, podman)
    # re-raise the same exception, resulting in N copies of the upstream error.
    # Consider: skip downstream tasks silently or return a sentinel value instead
    # of re-raising, so only the original upstream error surfaces once.
    async def setup_proxy_with_supervisor(*, buildbuddy_configured: bool = False) -> proxy_setup.ProxySetup:
        """Set up auth proxy (depends on supervisor)."""
        supervisor_result = await supervisor_task
        return await proxy_setup.setup_auth_proxy(
            settings, supervisor_result.client, buildbuddy_configured=buildbuddy_configured
        )

    async def setup_podman_with_deps() -> podman_service.PodmanSetup:
        """Set up podman (depends on supervisor + tmpfs mount)."""
        supervisor_result = await supervisor_task
        # tmpfs failure is non-fatal — podman falls back to VFS on 9p
        try:
            tmpfs_root = await tmpfs_mount_task
        except Exception as e:
            logger.warning("tmpfs mount failed, podman will use VFS on 9p: %s", e)
            tmpfs_root = None
        return await podman_service.setup_podman(settings, supervisor_result.client, tmpfs_root=tmpfs_root)

    async def setup_bazel_on_tmpfs() -> tmpfs_setup.TmpfsSetup:
        """Set up Bazel cache on tmpfs (depends on tmpfs mount)."""
        tmpfs_root = await tmpfs_mount_task
        return tmpfs_setup.setup_bazel_cache(tmpfs_root)

    def install_bazelisk_wrapper() -> bazelisk_setup.BazeliskSetup:
        """Install bazelisk and wrapper as separate tasks.

        Always installs the wrapper. Optionally downloads bazelisk unless
        DUCKTAPE_CLAUDE_HOOKS_INSTALL_BAZELISK is False.
        """
        wrapper_path = bazelisk_setup.install_wrapper(settings)
        skipped = not settings.install_bazelisk
        if not skipped:
            bazelisk_setup.install_bazelisk(settings)
        else:
            logger.info("Skipping bazelisk download (install_bazelisk=False)")
        return bazelisk_setup.BazeliskSetup(
            bazelisk_path=settings.get_bazelisk_path(),
            wrapper_path=wrapper_path,
            settings=settings,
            bazelisk_skipped=skipped,
        )

    # Decrypt age-encrypted secrets from .claude_hooks/secrets/ in the repo checkout.
    secrets_dir = project_dir / _HOOKS_DOTDIR / "secrets"
    secrets = secrets_setup.setup_secrets(age_key=settings.secrets_age_key, secrets_dir=secrets_dir)

    # Setup kubeconfig if KUBECONFIG_B64 is in decrypted secrets
    if secrets and "KUBECONFIG_B64" in secrets.env_vars:
        kubeconfig_setup.setup_kubeconfig(cache_dir=settings.get_cache_dir(), env_vars=secrets.env_vars)

    # Run BuildBuddy setup first so we know if RBE is available
    buildbuddy_result = await run_in_thread(
        lambda: buildbuddy_setup.setup_buildbuddy(
            project_dir, api_key=secrets.env_vars.get("BUILDBUDDY_API_KEY") if secrets else None
        )
    )
    buildbuddy_configured = (
        isinstance(buildbuddy_result, buildbuddy_setup.BuildbuddySetup) and buildbuddy_result.configured
    )

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    # Dependency graph:
    #   supervisor_task ──┬── proxy_task ──── setup_mkcert_with_proxy
    #                     └── setup_podman_with_deps ←── tmpfs_mount_task
    #   tmpfs_mount_task ──── setup_bazel_on_tmpfs
    logger.info("Starting parallel installations...")

    # Create proxy task with BuildBuddy configuration state
    proxy_task = asyncio.create_task(setup_proxy_with_supervisor(buildbuddy_configured=buildbuddy_configured))

    async def setup_mkcert_with_proxy() -> mkcert_setup.MkcertSetup:
        """Set up mkcert (depends on proxy for the combined CA bundle)."""
        if not settings.install_mkcert:
            raise SkipError("mkcert disabled (install_mkcert=False)")
        await proxy_task
        combined_ca = settings.get_auth_proxy_combined_ca()
        return mkcert_setup.setup_mkcert(settings, combined_ca if combined_ca.exists() else None)

    results = await asyncio.gather(
        proxy_task,
        setup_podman_with_deps(),
        run_in_thread(precommit_setup.install_precommit, project_dir),
        run_in_thread(nix_setup.install_nix, settings),
        run_in_thread(install_bazelisk_wrapper),
        setup_bazel_on_tmpfs(),
        setup_mkcert_with_proxy(),
        run_in_thread(cli_tools_setup.install_cli_tools, settings.get_wrapper_dir()),
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    auth_proxy: proxy_setup.ProxySetup | BaseException = results[0]
    podman: podman_service.PodmanSetup | BaseException = results[1]
    precommit: precommit_setup.PrecommitSetup | BaseException = results[2]
    nix: nix_setup.NixSetup | BaseException = results[3]
    bazelisk: bazelisk_setup.BazeliskSetup | BaseException = results[4]
    tmpfs: tmpfs_setup.TmpfsSetup | BaseException = results[5]
    mkcert: mkcert_setup.MkcertSetup | BaseException = results[6]
    cli_tools_result: list[str] | BaseException = results[7]
    # buildbuddy was set up earlier (before parallel tasks)
    buildbuddy: buildbuddy_setup.BuildbuddySetup | BaseException = buildbuddy_result

    # Log non-critical failures
    if isinstance(precommit, BaseException):
        logger.warning("Failed to install git pre-commit: %s", precommit)
    if isinstance(bazelisk, BaseException):
        logger.warning("Failed to install bazelisk: %s", bazelisk)
    if isinstance(buildbuddy, BaseException):
        logger.warning("Failed to configure BuildBuddy: %s", buildbuddy)
    if isinstance(tmpfs, BaseException):
        logger.warning("Failed to set up tmpfs caches: %s", tmpfs)
    if isinstance(mkcert, SkipError):
        logger.info("mkcert setup skipped: %s", mkcert)
    elif isinstance(mkcert, BaseException):
        logger.warning("Failed to set up mkcert: %s", mkcert)
    if isinstance(cli_tools_result, BaseException):
        logger.warning("Failed to install CLI tools: %s", cli_tools_result)

    # Handle nix result
    if isinstance(nix, SkipError):
        logger.info("Nix setup skipped: %s", nix)
    elif isinstance(nix, BaseException):
        logger.warning("Failed to install nix: %s", nix)

    # Handle podman result
    docker_host: str | None = None
    podman_env: dict[str, str] | None = None
    if isinstance(podman, SkipError):
        logger.info("Podman setup skipped: %s", podman)
    elif isinstance(podman, BaseException):
        logger.warning("Failed to configure podman: %s", podman)
    else:
        docker_host = podman.socket_url
        podman_env = podman.env_vars

    # Generate timestamp
    hook_timestamp = datetime.now()
    timestamp_file = settings.get_cache_dir() / "session-hook-last-run"
    timestamp_file.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Proxy setup is required - propagate failure with clear error message
    if isinstance(auth_proxy, BaseException):
        logger.error("Proxy setup failed: %s", auth_proxy)
        raise RuntimeError(f"Proxy setup failed: {auth_proxy}") from auth_proxy

    # Verify combined CA was created (sanity check - should always exist after successful proxy setup)
    combined_ca = settings.get_auth_proxy_combined_ca()
    if not combined_ca.exists():
        raise RuntimeError("Combined CA bundle not found - proxy setup incomplete")

    nix_paths = nix.paths if isinstance(nix, nix_setup.NixSetup) else []

    # Determine bazelisk_path: use system_bazel if install_bazelisk=False, otherwise downloaded bazelisk
    if isinstance(bazelisk, bazelisk_setup.BazeliskSetup) and bazelisk.bazelisk_skipped:
        if settings.system_bazel is not None:
            bazelisk_path = settings.system_bazel
        else:
            # Auto-detect system bazelisk/bazel
            auto_bazel = shutil.which("bazelisk") or shutil.which("bazel")
            if not auto_bazel:
                raise RuntimeError("install_bazelisk=False but no bazelisk/bazel found on PATH")
            bazelisk_path = Path(auto_bazel)
    else:
        bazelisk_path = settings.get_bazelisk_path()

    mkcert_cert: Path | None = None
    mkcert_key: Path | None = None
    if isinstance(mkcert, mkcert_setup.MkcertSetup):
        mkcert_cert = mkcert.cert_path
        mkcert_key = mkcert.key_path

    env_vars = env_file.EnvVars(
        proxy_port=settings.get_auth_proxy_port(),
        supervisor_port=settings.get_supervisor_port(),
        repo_root=project_dir,
        combined_ca=combined_ca,
        bazel_wrapper_dir=settings.get_wrapper_dir(),
        bazelisk_path=bazelisk_path,
        session_bazelrc=settings.get_auth_proxy_rc(),
        nix_paths=nix_paths,
        docker_host=docker_host,
        podman_env=podman_env,
        hook_timestamp=hook_timestamp,
        secrets_exports=secrets.env_exports if secrets else None,
        mkcert_cert=mkcert_cert,
        mkcert_key=mkcert_key,
    )

    # Write environment file ONCE
    env_file.write_env_file(env_file_path, env_vars)
    logger.info("Wrote environment to %s", env_file_path)

    # Emit status to log
    if isinstance(bazelisk, SkipError):
        bazel_status = "skipped"
    elif isinstance(bazelisk, BaseException):
        bazel_status = "not installed"
    else:
        bazel_status = bazelisk.status
    logger.info("Ready: bazel=%s, proxy=%s, CA=%s", bazel_status, auth_proxy.status, auth_proxy.ca_status)
    logger.info("Nix: %s", get_nix_status())
    if not isinstance(podman, BaseException):
        logger.info("Podman: %s", podman.status)

    # Render agent context from structured results
    emit_session_context(
        collector=collector,
        log_file=log_file,
        project_dir=project_dir,
        auth_proxy=auth_proxy,
        podman=None if isinstance(podman, BaseException) else podman,
        precommit=None if isinstance(precommit, BaseException) else precommit,
        secrets=secrets,
        mkcert=None if isinstance(mkcert, BaseException) else mkcert,
    )


async def async_main() -> None:
    """Async entry point: dispatch to web or CLI mode based on environment."""
    raw_input = sys.stdin.read()
    try:
        hook_input = HookInput.model_validate_json(raw_input)
    except Exception as e:
        print(f"Failed to parse hook input: {e}", file=sys.stderr)
        print(f"Raw input JSON:\n{raw_input}", file=sys.stderr)
        raise

    settings = HookSettings()

    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        await run_web_mode(hook_input, settings)
    else:
        await run_cli_mode(hook_input, settings)


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
