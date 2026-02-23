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
from opentelemetry import trace
from pydantic import BaseModel

from env_utils import env_utils
from tools.build_info import get_build_info
from tools.claude_hooks import (
    bazelisk_setup,
    buildbuddy_setup,
    cli_tools_setup,
    container_runtime,
    env_file,
    fork_remote_setup,
    kubeconfig_setup,
    mkcert_setup,
    nix_setup,
    otel,
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
from tools.claude_hooks.tracing import init_tracing, shutdown_tracing

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


def _get_local_registry_path() -> Path | None:
    """Get local registry path if it exists in the project directory.

    Used by bazelrc rendering to configure local registry with patched ape module.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return None
    local_registry = Path(project_dir) / "tools" / "local_registry"
    if local_registry.exists() and (local_registry / "bazel_registry.json").exists():
        return local_registry
    return None


def _render_session_bazelrc(
    session_dir: Path,
    *,
    web_proxy: bool,
    proxy_port: int | None,
    truststore_path: Path | None,
    truststore_password: str | None,
    local_proxy: str | None,
    combined_ca_path: Path | None,
    local_registry_path: Path | None,
    buildbuddy_configured: bool,
    bazel_cache_dir: Path | None = None,
) -> Path:
    """Render bazelrc.mako template to session_dir/bazelrc.

    Unified rendering engine for both modes:
    - CLI mode: web_proxy=False, all optional params are None
    - Web mode: web_proxy=True with proxy configuration parameters
    """
    template = Template(CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"])
    result: str = template.render(
        web_proxy=web_proxy,
        proxy_port=proxy_port,
        truststore_path=truststore_path,
        truststore_password=truststore_password,
        local_proxy=local_proxy,
        combined_ca_path=combined_ca_path,
        local_registry_path=local_registry_path,
        buildbuddy_configured=buildbuddy_configured,
        bazel_cache_dir=bazel_cache_dir,
    )
    bazelrc_path = session_dir / "bazelrc"
    write_config(bazelrc_path, result, "session bazelrc")
    return bazelrc_path


# ============================================================================
# CLI mode: per-session bazel wrapper with direnv integration
# ============================================================================


async def run_cli_mode(hook_input: HookInput, settings: HookSettings, env_file_path: Path) -> None:
    """CLI mode: set up per-session bazel wrapper and direnv eval.

    Renders a per-session bazelrc (with --config=ai_agent for quiet output),
    installs a bazel/bazelisk wrapper, and writes CLAUDE_ENV_FILE with the
    wrapper on PATH and direnv eval for .envrc propagation.
    """
    _collector, log_file = setup_logging(settings, print_banner=False)
    tracer, trace_file = init_tracing(hook_input.session_id, settings.session_dir)

    with tracer.start_as_current_span(
        "session_start_cli", attributes={"session.id": hook_input.session_id, "hook.source": hook_input.source}
    ):
        logger.info("Session start hook (CLI mode)")
        logger.info("Hook input: %s", hook_input.model_dump_json())
        log_entrypoint_debug("session_start")

        with tracer.start_as_current_span("render_bazelrc"):
            session_bazelrc = _render_session_bazelrc(
                settings.session_dir,
                web_proxy=False,
                proxy_port=None,
                truststore_path=None,
                truststore_password=None,
                local_proxy=None,
                combined_ca_path=None,
                local_registry_path=None,
                buildbuddy_configured=False,
            )

        with tracer.start_as_current_span("install_bazel_wrappers"):
            bazelisk_setup.install_wrapper(settings)

        with tracer.start_as_current_span("write_env_file"):
            env_file.write_env_file(
                env_file_path,
                env_file.EnvVars(
                    bazel_wrapper_dir=settings.get_wrapper_dir(), session_bazelrc=session_bazelrc, with_direnv=True
                ),
            )

        logger.info("CLI session configured: %s", settings.session_dir)
        print(f"Claude Code CLI session configured (log: {log_file})")

    shutdown_tracing()
    logger.info("Trace file: %s", trace_file)


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


def _render_extra_context(
    project_dir: Path,
    secrets: secrets_setup.SecretsSetup | None,
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None,
) -> str:
    """Render repo-specific context from .claude_hooks/templates/context.mako if it exists."""
    extra_template_path = project_dir / _HOOKS_DOTDIR / "templates" / "context.mako"
    if not extra_template_path.exists():
        return ""
    template = Template(extra_template_path.read_text())
    result: str = template.render(secrets=secrets, fork_result=fork_result)
    return result.rstrip("\n")


def emit_session_context(
    collector: LogCollector,
    log_file: Path,
    project_dir: Path,
    auth_proxy: proxy_setup.ProxySetup,
    container: container_runtime.ContainerRuntimeSetup | None,
    precommit: precommit_setup.PrecommitSetup | None,
    secrets: secrets_setup.SecretsSetup | None,
    mkcert: mkcert_setup.MkcertSetup | None = None,
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None,
) -> None:
    """Emit compact context summary for Claude Code transcript.

    Renders the generic session_context.mako (from the wheel) with structured
    setup results, then appends repo-specific context from
    .claude_hooks/templates/context.mako if it exists.
    """
    status = "ERRORS" if collector.has_errors else "OK with warnings" if collector.has_warnings else "OK"

    extra_context = _render_extra_context(project_dir, secrets, fork_result)

    template = Template((_TEMPLATES_DIR / "session_context.mako").read_text())
    result: str = template.render(
        WARNING=logging.WARNING,
        build_commit=get_build_info().commit,
        status=status,
        proxy=auth_proxy,
        container=container,
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
    log_file = settings.get_log_file()

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


async def run_web_mode(hook_input: HookInput, settings: HookSettings, env_file_path: Path) -> None:
    """Web mode with parallelized operations.

    Uses asyncio to parallelize independent installations (git hook, cluster
    tools, nix) while maintaining correct sequencing for dependent operations.

    Writes CLAUDE_ENV_FILE once at the end with all collected environment
    variables.
    """
    collector, log_file = setup_logging(settings)
    tracer, trace_file = init_tracing(hook_input.session_id, settings.session_dir)
    root_span = tracer.start_span(
        "session_start_web", attributes={"session.id": hook_input.session_id, "hook.source": hook_input.source}
    )
    root_ctx = trace.set_span_in_context(root_span)

    logger.info("Session start hook")
    logger.info("Hook: %s", __file__)
    logger.info("Log:  %s", log_file)
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")
    logger.info("Setting up dev environment...")
    logger.info(format_environment_summary())

    # Get required project directory
    project_dir = env_utils.get_required_env_path("CLAUDE_PROJECT_DIR")
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)
    logger.info("Session directory: %s", settings.session_dir)

    # --- Traced async helpers ---
    # Each helper creates its own child span under root_ctx so the parallel
    # tasks all show up as direct children of the root span.

    async def traced_supervisor_start():
        with tracer.start_as_current_span("supervisor_start", context=root_ctx):
            return await supervisor_setup.start(settings)

    # Start supervisor (required by proxy and podman)
    supervisor_task = asyncio.create_task(traced_supervisor_start())

    async def mount_tmpfs_at(path: Path) -> bool:
        """Mount a tmpfs at the given path. Returns True on success, False on failure."""
        path.mkdir(parents=True, exist_ok=True)
        try:
            await run_in_thread(tmpfs_setup.ensure_tmpfs_mounted, path)
            return True
        except Exception as e:
            logger.warning("tmpfs mount failed at %s, will fall back to 9p: %s", path, e)
            return False

    # Wrappers that depend on supervisor being ready
    # TODO: Handle upstream dependency failures more gracefully.
    # Currently, when supervisor_task fails, all downstream tasks (proxy, podman)
    # re-raise the same exception, resulting in N copies of the upstream error.
    # Consider: skip downstream tasks silently or return a sentinel value instead
    # of re-raising, so only the original upstream error surfaces once.
    async def setup_proxy_with_supervisor(*, buildbuddy_configured: bool = False) -> proxy_setup.ProxySetup:
        """Set up auth proxy (depends on supervisor)."""
        with tracer.start_as_current_span("setup_proxy", context=root_ctx):
            supervisor_result = await supervisor_task
            return await proxy_setup.setup_auth_proxy(
                settings, supervisor_result.client, buildbuddy_configured=buildbuddy_configured
            )

    async def setup_container_runtime_task() -> container_runtime.ContainerRuntimeSetup:
        """Set up configured container runtime (depends on supervisor + per-component tmpfs)."""
        with tracer.start_as_current_span("setup_container_runtime", context=root_ctx):
            storage_dir = container_runtime.get_storage_dir(settings)
            if storage_dir is None:
                raise SkipError(f"Container runtime disabled (container_runtime={settings.container_runtime})")
            supervisor_result = await supervisor_task
            # tmpfs failure is non-fatal — runtime falls back to VFS on 9p
            tmpfs_mounted = await mount_tmpfs_at(storage_dir)
            return await container_runtime.setup_container_runtime(
                settings, supervisor_result.client, tmpfs_mounted=tmpfs_mounted
            )

    async def setup_bazel_on_tmpfs() -> tmpfs_setup.TmpfsSetup:
        """Set up Bazel cache (mounts dedicated tmpfs under session dir)."""
        with tracer.start_as_current_span("setup_bazel_tmpfs", context=root_ctx):
            bazel_cache_dir = settings.get_bazel_cache_dir()
            await mount_tmpfs_at(bazel_cache_dir)
            return tmpfs_setup.setup_bazel_cache(bazel_cache_dir)

    @tracer.start_as_current_span("install_bazelisk", context=root_ctx)
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
    with tracer.start_as_current_span("setup_secrets", context=root_ctx):
        secrets_dir = project_dir / _HOOKS_DOTDIR / "secrets"
        secrets = secrets_setup.setup_secrets(age_key=settings.secrets_age_key, secrets_dir=secrets_dir)

    # Run BuildBuddy setup first so we know if RBE is available
    with tracer.start_as_current_span("setup_buildbuddy", context=root_ctx):
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
    #                     └── setup_container_runtime (Docker or Podman)
    #                         (each runtime mounts its own tmpfs internally)
    #   setup_bazel_on_tmpfs mounts its own tmpfs independently
    logger.info("Starting parallel installations...")

    # Create proxy task with BuildBuddy configuration state
    proxy_task = asyncio.create_task(setup_proxy_with_supervisor(buildbuddy_configured=buildbuddy_configured))

    async def mkcert_generate_certs() -> mkcert_setup.MkcertSetup:
        """Generate mkcert certs (no proxy dependency — runs immediately in parallel)."""
        with tracer.start_as_current_span("setup_mkcert", context=root_ctx):
            if not settings.install_mkcert:
                raise SkipError("mkcert disabled (install_mkcert=False)")
            # Pass combined_ca=None: bundle append happens in mkcert_append_bundle
            return await mkcert_setup.setup_mkcert(settings, combined_ca=None)

    # Start cert generation immediately, without waiting for the proxy.
    mkcert_task = asyncio.create_task(mkcert_generate_certs())

    async def mkcert_append_bundle() -> mkcert_setup.MkcertSetup:
        """Append mkcert CA to the combined CA bundle (depends on proxy + cert gen)."""
        with tracer.start_as_current_span("mkcert_append_bundle", context=root_ctx):
            mkcert_result = await mkcert_task
            await proxy_task
            combined_ca = settings.get_auth_proxy_combined_ca()
            if combined_ca.exists():
                mkcert_setup.append_mkcert_ca_to_bundle(mkcert_result.ca_root, combined_ca)
            return mkcert_result

    async def traced_precommit():
        with tracer.start_as_current_span("install_precommit", context=root_ctx):
            return await run_in_thread(precommit_setup.install_precommit, project_dir, settings.session_dir)

    async def traced_nix():
        with tracer.start_as_current_span("install_nix", context=root_ctx):
            return await run_in_thread(nix_setup.install_nix, settings)

    async def traced_cli_tools():
        with tracer.start_as_current_span("install_cli_tools", context=root_ctx):
            return await run_in_thread(cli_tools_setup.install_cli_tools, settings.get_wrapper_dir())

    results = await asyncio.gather(
        proxy_task,
        setup_container_runtime_task(),
        traced_precommit(),
        traced_nix(),
        run_in_thread(install_bazelisk_wrapper),
        setup_bazel_on_tmpfs(),
        mkcert_append_bundle(),
        traced_cli_tools(),
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    auth_proxy: proxy_setup.ProxySetup | BaseException = results[0]
    container: container_runtime.ContainerRuntimeSetup | BaseException = results[1]
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

    # Handle container runtime result
    docker_env: dict[str, str] | None = None
    if isinstance(container, SkipError):
        logger.info("Container runtime setup skipped: %s", container)
    elif isinstance(container, BaseException):
        logger.warning("Failed to configure container runtime: %s", container)
    else:
        docker_env = container.env_vars

    # Generate timestamp
    hook_timestamp = datetime.now()
    timestamp_file = settings.session_dir / "session-hook-last-run"
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

    # Build kubeconfig now that the proxy CA is available to inject alongside the cluster CA.
    if secrets and secrets.kubeconfig:
        with tracer.start_as_current_span("setup_kubeconfig", context=root_ctx):
            proxy_ca_file = settings.get_auth_proxy_ca_file()
            proxy_ca_pem = proxy_ca_file.read_text() if proxy_ca_file.exists() else None
            kubeconfig_setup.setup_kubeconfig(
                session_dir=settings.session_dir,
                secret=secrets.kubeconfig,
                env_vars=secrets.env_vars,
                proxy_ca_pem=proxy_ca_pem,
            )

    # Ensure 'fork' git remote when GITHUB_TOKEN is available.
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None
    if secrets and "GITHUB_TOKEN" in secrets.env_vars:
        try:
            with tracer.start_as_current_span("setup_fork_remote", context=root_ctx):
                fork_result = fork_remote_setup.ensure_fork_remote(secrets.env_vars["GITHUB_TOKEN"], project_dir)
        except Exception as e:
            logger.warning("Fork remote setup failed: %s", e)

    # Render session bazelrc (unified for web mode with proxy configuration)
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        truststore = settings.get_auth_proxy_truststore()
        proxy_port = settings.get_auth_proxy_port()
        local_proxy = f"http://localhost:{proxy_port}"
        local_registry = _get_local_registry_path()
        if local_registry:
            logger.info("Found local registry at %s (patched ape for native ELF)", local_registry)

        session_bazelrc = _render_session_bazelrc(
            settings.session_dir,
            web_proxy=True,
            proxy_port=proxy_port,
            truststore_path=truststore,
            truststore_password=proxy_setup.TRUSTSTORE_PASSWORD,
            local_proxy=local_proxy,
            combined_ca_path=combined_ca,
            local_registry_path=local_registry,
            buildbuddy_configured=buildbuddy_configured,
            bazel_cache_dir=tmpfs.bazel_cache if isinstance(tmpfs, tmpfs_setup.TmpfsSetup) else None,
        )

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
        bazel_wrapper_dir=settings.get_wrapper_dir(),
        session_bazelrc=session_bazelrc,
        session_dir=settings.session_dir,
        proxy_port=settings.get_auth_proxy_port(),
        supervisor_port=settings.get_supervisor_port(),
        repo_root=project_dir,
        combined_ca=combined_ca,
        bazelisk_path=bazelisk_path,
        nix_paths=nix_paths,
        docker_env=docker_env,
        hook_timestamp=hook_timestamp,
        mkcert_cert=mkcert_cert,
        mkcert_key=mkcert_key,
        secrets_env_vars=secrets.env_vars if secrets else None,
    )

    # Write environment file ONCE
    with tracer.start_as_current_span("write_env_file", context=root_ctx):
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
    if not isinstance(container, BaseException):
        logger.info("%s: %s", container.runtime.capitalize(), container.status)

    with tracer.start_as_current_span("emit_session_context", context=root_ctx):
        emit_session_context(
            collector=collector,
            log_file=log_file,
            project_dir=project_dir,
            auth_proxy=auth_proxy,
            container=None if isinstance(container, BaseException) else container,
            precommit=None if isinstance(precommit, BaseException) else precommit,
            secrets=secrets,
            mkcert=None if isinstance(mkcert, BaseException) else mkcert,
            fork_result=fork_result,
        )

    root_span.end()
    shutdown_tracing()
    logger.info("Trace file: %s", trace_file)


async def async_main() -> None:
    """Async entry point: dispatch to web or CLI mode based on environment."""
    raw_input = sys.stdin.read()
    try:
        hook_input = HookInput.model_validate_json(raw_input)
    except Exception as e:
        print(f"Failed to parse hook input: {e}", file=sys.stderr)
        print(f"Raw input JSON:\n{raw_input}", file=sys.stderr)
        raise

    env_file_path = env_utils.get_required_env_path("CLAUDE_ENV_FILE")
    settings = HookSettings(session_dir=env_file_path.parent)
    otel.init(settings)

    if os.environ.get("CLAUDE_CODE_REMOTE") == "true":
        await run_web_mode(hook_input, settings, env_file_path)
    else:
        await run_cli_mode(hook_input, settings, env_file_path)


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
