"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy and git hooks.
CLI mode: Sets up per-session bazel wrapper with direnv integration.

Both modes render a per-session bazelrc from the unified bazelrc.mako template
and install a bazel wrapper that injects --bazelrc=<session-bazelrc>.
"""

import asyncio
import logging
import logging.handlers
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from mako.template import Template
from opentelemetry import trace

from devinfra.build_info import get_build_info
from devinfra.claude import (
    apt_setup,
    bazel_server_warmup,
    bazelisk_setup,
    buildbuddy_setup,
    cli_tools_setup,
    container_runtime,
    env_file,
    fork_remote_setup,
    k8s_secrets_setup,
    mkcert_setup,
    precommit_setup,
    tmpfs_setup,
)
from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.auth_proxy.proxy import AuthForwardingProxy
from devinfra.claude.claude_api.hooks.session_start import (
    SessionStartHookInput,
    SessionStartHookSpecificOutput,
    SessionStartOutput,
)
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.errors import SkipError
from devinfra.claude.hook_config import HOOKS_DOTDIR, HookConfig, OtelConfig
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter
from devinfra.claude.managed_files import write_config
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import CONFIG_FILES, HookSettings
from devinfra.claude.supervisor import setup as supervisor_setup

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


@dataclass(frozen=True)
class CallerContext:
    """Structured env vars extracted from the hook client's environment.

    Replaces passing raw dict[str, str] through the call stack. Extracted once
    at the session start entry point (handle), then threaded through.
    """

    env_file_path: Path
    web_mode: bool
    project_dir: Path

    @property
    def mode_label(self) -> str:
        return "web" if self.web_mode else "cli"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "CallerContext":
        env_file_str = env.get("CLAUDE_ENV_FILE")
        if not env_file_str:
            raise KeyError("CLAUDE_ENV_FILE environment variable is required")
        project_dir_str = env.get("CLAUDE_PROJECT_DIR")
        if not project_dir_str:
            raise KeyError("CLAUDE_PROJECT_DIR environment variable is required")
        return cls(
            env_file_path=Path(env_file_str),
            web_mode=env.get("CLAUDE_CODE_REMOTE") == "true",
            project_dir=Path(project_dir_str),
        )


# ============================================================================
# Platform setup result
# ============================================================================


@dataclass
class PlatformSetup:
    """Results of platform-specific setup (web or CLI).

    Carries everything from the platform-specific setup phase to the shared
    downstream steps (bazelrc render, env file write, session context emit).
    """

    # Bazelrc rendering params
    proxy_port: int | None = None
    truststore_path: Path | None = None
    truststore_password: str | None = None
    local_proxy: str | None = None
    combined_ca_path: Path | None = None
    bazel_cache_dir: Path | None = None

    # EnvVars params
    session_dir: Path | None = None
    supervisor_port: int | None = None
    bazelisk_path: Path | None = None
    docker_env: dict[str, str] | None = None
    mkcert_cert: Path | None = None
    mkcert_key: Path | None = None
    secrets_env_vars: dict[str, str] | None = None
    with_direnv: bool = False

    # Session context params
    auth_proxy: proxy_setup.ProxySetup | None = None
    container: container_runtime.ContainerRuntimeSetup | None = None
    precommit: precommit_setup.PrecommitSetup | None = None
    secrets: k8s_secrets_setup.K8sSecretsResult | None = None
    mkcert: mkcert_setup.MkcertSetup | None = None
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None
    buildbuddy_configured: bool = False


# ============================================================================
# Shared helpers
# ============================================================================


def _render_extra_context(
    project_dir: Path,
    secrets: k8s_secrets_setup.K8sSecretsResult | None,
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None,
) -> str:
    """Render repo-specific context from .claude_hooks/templates/context.mako if it exists."""
    extra_template_path = project_dir / HOOKS_DOTDIR / "templates" / "context.mako"
    if not extra_template_path.exists():
        return ""
    template = Template(extra_template_path.read_text())
    result: str = template.render(secrets=secrets, fork_result=fork_result)
    return result.rstrip("\n")


class LogCollector(logging.handlers.MemoryHandler):
    """Collects log records from session start for the mako template output.

    Uses MemoryHandler with high capacity and no auto-flush to buffer all records.
    The collected warnings/errors are rendered into the session context banner.
    """

    def __init__(self) -> None:
        super().__init__(capacity=1000, flushLevel=logging.CRITICAL + 1)

    @property
    def has_errors(self) -> bool:
        return any(r.levelno >= logging.ERROR for r in self.buffer)

    @property
    def has_warnings(self) -> bool:
        return any(r.levelno == logging.WARNING for r in self.buffer)


def _setup_session_logging() -> LogCollector:
    """Set up a LogCollector for session start output.

    Session start runs inside the hook daemon, which already configures
    file logging to daemon.log.  We only add a LogCollector to capture
    warnings/errors for the session context banner.
    """
    formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    collector = LogCollector()
    collector.setFormatter(formatter)
    logging.getLogger().addHandler(collector)

    return collector


# ============================================================================
# Async helpers
# ============================================================================


async def run_in_thread(func, *args):
    """Run blocking function in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args)


# ============================================================================
# Platform-specific setup
# ============================================================================


async def _setup_web(
    paths: SessionPaths,
    settings: HookSettings,
    project_dir: Path,
    tracer: trace.Tracer,
    root_ctx: trace.Context,
    hook_config: k8s_secrets_setup.HookConfig | None,
    http: httpx.Client,
    proxy: AuthForwardingProxy,
) -> PlatformSetup:
    """Web mode: supervisor, proxy, containers, secrets, parallel installs.

    Returns a fully populated PlatformSetup with all results needed by the
    shared downstream steps.
    """
    logger.info("Setting up dev environment...")

    async def traced_supervisor_start():
        with tracer.start_as_current_span("supervisor_start", context=root_ctx):
            return await supervisor_setup.start(paths, settings)

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

    async def setup_proxy_credentials() -> proxy_setup.ProxySetup:
        """Write proxy credentials and set up CA/truststore (proxy already running in-process)."""
        with tracer.start_as_current_span("setup_proxy", context=root_ctx):
            return await proxy_setup.setup_auth_proxy(paths, settings, proxy=proxy)

    async def setup_container_runtime_task() -> container_runtime.ContainerRuntimeSetup:
        """Set up configured container runtime (depends on supervisor + apt for podman)."""
        with tracer.start_as_current_span("setup_container_runtime", context=root_ctx):
            storage_dir = container_runtime.get_storage_dir(paths, settings)
            if storage_dir is None:
                raise SkipError(f"Container runtime disabled (container_runtime={settings.container_runtime})")
            # Podman needs apt packages installed first.
            if settings.container_runtime == "podman":
                await apt_task
            supervisor_result = await supervisor_task
            # tmpfs failure is non-fatal — runtime falls back to VFS on 9p
            tmpfs_mounted = await mount_tmpfs_at(storage_dir)
            return await container_runtime.setup_container_runtime(
                paths, settings, supervisor_result.client, tmpfs_mounted=tmpfs_mounted
            )

    async def setup_bazel_on_tmpfs() -> tmpfs_setup.TmpfsSetup:
        """Set up Bazel cache (mounts dedicated tmpfs under session dir)."""
        with tracer.start_as_current_span("setup_bazel_tmpfs", context=root_ctx):
            bazel_cache_dir = paths.bazel_cache_dir
            await mount_tmpfs_at(bazel_cache_dir)
            return tmpfs_setup.setup_bazel_cache(bazel_cache_dir)

    @tracer.start_as_current_span("install_bazelisk", context=root_ctx)
    def install_bazelisk_wrapper() -> bazelisk_setup.BazeliskSetup:
        """Install bazelisk and wrapper.

        Always installs the wrapper. Optionally downloads bazelisk unless
        DUCKTAPE_CLAUDE_HOOKS_INSTALL_BAZELISK is False.
        """
        wrapper_path = bazelisk_setup.install_wrapper(paths)
        skipped = not settings.install_bazelisk
        if not skipped:
            bazelisk_setup.install_bazelisk(paths, http)
        else:
            logger.info("Skipping bazelisk download (install_bazelisk=False)")
        return bazelisk_setup.BazeliskSetup(
            bazelisk_path=paths.bazelisk_path, wrapper_path=wrapper_path, paths=paths, bazelisk_skipped=skipped
        )

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    # Dependency graph:
    #   apt_task (no deps — runs immediately)
    #   proxy_task (in-process, no supervisor dependency)
    #   supervisor_task + apt_task ── setup_container_runtime (Docker or Podman)
    #                                 (each runtime mounts its own tmpfs internally)
    #   setup_bazel_on_tmpfs mounts its own tmpfs independently
    logger.info("Starting parallel installations...")

    # Consolidated apt install: native dev headers (always) + podman (if needed).
    apt_packages = list(apt_setup.NATIVE_DEV_PACKAGES)
    if settings.container_runtime == "podman" and shutil.which("podman") is None:
        apt_packages.extend(apt_setup.PODMAN_PACKAGES)

    @tracer.start_as_current_span("install_apt_packages", context=root_ctx)
    async def traced_apt_setup():
        return await apt_setup.install_packages(apt_packages)

    apt_task = asyncio.create_task(traced_apt_setup())

    # Proxy task starts without BuildBuddy state (buildbuddy setup depends on
    # k8s secrets which in turn depend on proxy being up for TLS).
    # Proxy runs in-process (daemon threads, started by hook daemon server).
    # This task writes credentials and sets up CA/truststore.
    proxy_task = asyncio.create_task(setup_proxy_credentials())

    async def mkcert_generate_certs() -> mkcert_setup.MkcertSetup:
        """Generate mkcert certs (no proxy dependency — runs immediately in parallel)."""
        with tracer.start_as_current_span("setup_mkcert", context=root_ctx):
            if not settings.install_mkcert:
                raise SkipError("mkcert disabled (install_mkcert=False)")
            # Pass combined_ca=None: bundle append happens in mkcert_append_bundle
            return await mkcert_setup.setup_mkcert(paths, combined_ca=None, http=http)

    # Start cert generation immediately, without waiting for the proxy.
    mkcert_task = asyncio.create_task(mkcert_generate_certs())

    async def mkcert_append_bundle() -> mkcert_setup.MkcertSetup:
        """Append mkcert CA to the combined CA bundle (depends on proxy + cert gen)."""
        with tracer.start_as_current_span("mkcert_append_bundle", context=root_ctx):
            mkcert_result = await mkcert_task
            await proxy_task
            combined_ca = paths.auth_proxy_combined_ca
            if combined_ca.exists():
                mkcert_setup.append_mkcert_ca_to_bundle(mkcert_result.ca_root, combined_ca)
            return mkcert_result

    @tracer.start_as_current_span("install_precommit", context=root_ctx)
    async def traced_precommit():
        return await run_in_thread(precommit_setup.install_precommit, project_dir, paths.session_dir)

    @tracer.start_as_current_span("install_cli_tools", context=root_ctx)
    async def traced_cli_tools():
        return await run_in_thread(cli_tools_setup.install_cli_tools, paths.wrapper_dir, http)

    results = await asyncio.gather(
        proxy_task,
        setup_container_runtime_task(),
        traced_precommit(),
        run_in_thread(install_bazelisk_wrapper),
        setup_bazel_on_tmpfs(),
        mkcert_append_bundle(),
        traced_cli_tools(),
        apt_task,
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    auth_proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    container_result: container_runtime.ContainerRuntimeSetup | BaseException = results[1]
    precommit_result: precommit_setup.PrecommitSetup | BaseException = results[2]
    bazelisk_result: bazelisk_setup.BazeliskSetup | BaseException = results[3]
    tmpfs_result: tmpfs_setup.TmpfsSetup | BaseException = results[4]
    mkcert_result: mkcert_setup.MkcertSetup | BaseException = results[5]
    cli_tools_result: list[str] | BaseException = results[6]
    apt_result: apt_setup.AptSetup | BaseException = results[7]

    # Log non-critical failures
    if isinstance(precommit_result, BaseException):
        logger.warning("Failed to install git pre-commit: %s", precommit_result)
    if isinstance(bazelisk_result, BaseException):
        logger.warning("Failed to install bazelisk: %s", bazelisk_result)
    if isinstance(tmpfs_result, BaseException):
        logger.warning("Failed to set up tmpfs caches: %s", tmpfs_result)
    if isinstance(mkcert_result, SkipError):
        logger.info("mkcert setup skipped: %s", mkcert_result)
    elif isinstance(mkcert_result, BaseException):
        logger.warning("Failed to set up mkcert: %s", mkcert_result)
    if isinstance(cli_tools_result, BaseException):
        logger.warning("Failed to install CLI tools: %s", cli_tools_result)
    if isinstance(apt_result, BaseException):
        logger.warning("Failed to install system packages: %s", apt_result)

    # Handle container runtime result
    docker_env: dict[str, str] | None = None
    if isinstance(container_result, SkipError):
        logger.info("Container runtime setup skipped: %s", container_result)
    elif isinstance(container_result, BaseException):
        logger.warning("Failed to configure container runtime: %s", container_result)
    else:
        docker_env = container_result.env_vars

    # Proxy setup is required - propagate failure with clear error message
    if isinstance(auth_proxy_result, BaseException):
        logger.error("Proxy setup failed: %s", auth_proxy_result)
        raise RuntimeError(f"Proxy setup failed: {auth_proxy_result}") from auth_proxy_result

    combined_ca = paths.auth_proxy_combined_ca

    # Read k8s secrets now that combined CA is available for TLS.
    # Route through the auth proxy so the upstream egress proxy gets credentials.
    secrets: k8s_secrets_setup.K8sSecretsResult | None = None
    if settings.k8s_token and hook_config:
        with tracer.start_as_current_span("setup_k8s_secrets", context=root_ctx):
            secrets = k8s_secrets_setup.setup_k8s_secrets(
                token=settings.k8s_token,
                session_dir=paths.session_dir,
                combined_ca_path=combined_ca,
                config=hook_config,
                proxy=f"http://localhost:{settings.auth_proxy_port}",
            )

    # Configure BuildBuddy now that k8s secrets (with API key) are available.
    buildbuddy_api_key = secrets.buildbuddy_api_key if secrets else None
    with tracer.start_as_current_span("setup_buildbuddy", context=root_ctx):
        buildbuddy_result = await run_in_thread(lambda: buildbuddy_setup.setup_buildbuddy(api_key=buildbuddy_api_key))
    buildbuddy_configured = (
        isinstance(buildbuddy_result, buildbuddy_setup.BuildbuddySetup) and buildbuddy_result.configured
    )
    if isinstance(buildbuddy_result, BaseException):
        logger.warning("Failed to configure BuildBuddy: %s", buildbuddy_result)

    # Ensure 'fork' git remote when GITHUB_TOKEN is available.
    fork_result: fork_remote_setup.ForkRemoteSetup | None = None
    if secrets and "GITHUB_TOKEN" in secrets.env_vars:
        try:
            with tracer.start_as_current_span("setup_fork_remote", context=root_ctx):
                fork_result = fork_remote_setup.ensure_fork_remote(secrets.env_vars["GITHUB_TOKEN"], project_dir)
        except Exception as e:
            logger.warning("Fork remote setup failed: %s", e)

    # Determine bazelisk_path: use system_bazel if install_bazelisk=False, otherwise downloaded bazelisk
    bazelisk_path: Path | None
    if isinstance(bazelisk_result, bazelisk_setup.BazeliskSetup) and bazelisk_result.bazelisk_skipped:
        if settings.system_bazel is not None:
            bazelisk_path = Path(settings.system_bazel)
        else:
            # Auto-detect system bazelisk/bazel
            auto_bazel = shutil.which("bazelisk") or shutil.which("bazel")
            if not auto_bazel:
                raise RuntimeError("install_bazelisk=False but no bazelisk/bazel found on PATH")
            bazelisk_path = Path(auto_bazel)
    else:
        bazelisk_path = paths.bazelisk_path

    logger.info(
        "Ready: bazel=%s, proxy=%s, CA=%s", bazelisk_result, auth_proxy_result.status, auth_proxy_result.ca_status
    )
    logger.info("Container: %s", container_result)

    return PlatformSetup(
        # Bazelrc rendering
        proxy_port=settings.auth_proxy_port,
        truststore_path=paths.auth_proxy_truststore,
        truststore_password=proxy_setup.TRUSTSTORE_PASSWORD,
        local_proxy=f"http://localhost:{settings.auth_proxy_port}",
        combined_ca_path=combined_ca,
        bazel_cache_dir=tmpfs_result.bazel_cache if isinstance(tmpfs_result, tmpfs_setup.TmpfsSetup) else None,
        # EnvVars
        session_dir=paths.session_dir,
        supervisor_port=settings.supervisor_port,
        bazelisk_path=bazelisk_path,
        docker_env=docker_env,
        mkcert_cert=mkcert_result.cert_path if isinstance(mkcert_result, mkcert_setup.MkcertSetup) else None,
        mkcert_key=mkcert_result.key_path if isinstance(mkcert_result, mkcert_setup.MkcertSetup) else None,
        secrets_env_vars=secrets.env_vars if secrets else None,
        # Session context
        auth_proxy=auth_proxy_result,
        container=None if isinstance(container_result, BaseException) else container_result,
        precommit=None if isinstance(precommit_result, BaseException) else precommit_result,
        secrets=secrets,
        mkcert=None if isinstance(mkcert_result, BaseException) else mkcert_result,
        fork_result=fork_result,
        buildbuddy_configured=buildbuddy_configured,
    )


# ============================================================================
# Unified session entry point
# ============================================================================


async def run_session(
    hook_input: SessionStartHookInput,
    paths: SessionPaths,
    settings: HookSettings,
    ctx: CallerContext,
    http: httpx.Client,
    otlp_exporter: DeferredOtlpExporter,
    proxy: AuthForwardingProxy | None,
    background_tasks: set[asyncio.Task[object]] | None = None,
) -> SessionStartOutput:
    """Unified session setup for both web and CLI modes.

    Dispatches platform-specific setup, then runs shared steps:
    bazelrc render, wrapper install, env file write, session context emit.
    """

    collector = _setup_session_logging()
    log_file = paths.hook_daemon_dir / "daemon.log"
    tracer = trace.get_tracer(__name__)
    root_span = tracer.start_span(
        "session_start",
        attributes={"session.id": hook_input.session_id, "hook.source": hook_input.source, "mode": ctx.mode_label},
    )
    root_ctx = trace.set_span_in_context(root_span)

    logger.info("Session start hook (%s mode)", ctx.mode_label)
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")

    project_dir = ctx.project_dir
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)
    logger.info("Session directory: %s", paths.session_dir)

    # Load hook config (general config file, not gated on k8s_token).
    hook_config = HookConfig.load_from_repo(project_dir)

    # K8s secrets are read after platform setup (proxy must be up for web mode TLS).
    secrets: k8s_secrets_setup.K8sSecretsResult | None = None

    # Platform-specific setup
    if ctx.web_mode:
        assert proxy is not None, "proxy must be running in web mode"
        setup = await _setup_web(paths, settings, project_dir, tracer, root_ctx, hook_config, http=http, proxy=proxy)
    else:
        # CLI mode: read k8s secrets (no proxy needed, combined_ca_path=None).
        if settings.k8s_token and hook_config:
            secrets = k8s_secrets_setup.setup_k8s_secrets(
                token=settings.k8s_token, session_dir=paths.session_dir, combined_ca_path=None, config=hook_config
            )
        setup = PlatformSetup(
            buildbuddy_configured=buildbuddy_setup.is_buildbuddy_configured(),
            with_direnv=True,
            secrets=secrets,
            secrets_env_vars=secrets.env_vars if secrets else None,
        )

    # Configure OTLP now that k8s secrets (with bearer token) are available.
    # Bearer token from k8s overrides config file / env var. Idempotent across sessions.
    if hook_config and hook_config.otel:
        otel_config = hook_config.otel.with_env_overrides()
        otel_token = setup.secrets.otel_bearer_token if setup.secrets else None
        if otel_token:
            otel_config = OtelConfig(endpoint=otel_config.endpoint, bearer_token=otel_token)
        otlp_exporter.configure(otel_config)

    # Render session bazelrc
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        bazelrc_template = Template(
            CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"]
        )
        bazelrc_content: str = bazelrc_template.render(
            web_proxy=ctx.web_mode,
            proxy_port=setup.proxy_port,
            truststore_path=setup.truststore_path,
            truststore_password=setup.truststore_password,
            local_proxy=setup.local_proxy,
            combined_ca_path=setup.combined_ca_path,
            buildbuddy_configured=setup.buildbuddy_configured,
            buildbuddy_bazelrc=buildbuddy_setup.BUILDBUDDY_BAZELRC,
            bazel_cache_dir=setup.bazel_cache_dir,
        )
        session_bazelrc = paths.session_dir / "bazelrc"
        write_config(session_bazelrc, bazelrc_content, "session bazelrc")

    # Install bazel wrapper (web mode already downloaded bazelisk in parallel)
    with tracer.start_as_current_span("install_bazel_wrappers", context=root_ctx):
        bazelisk_setup.install_wrapper(paths)

    # Generate timestamp
    hook_timestamp = datetime.now()
    timestamp_file = paths.session_dir / "session-hook-last-run"
    timestamp_file.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Write environment file
    with tracer.start_as_current_span("write_env_file", context=root_ctx):
        env_vars = env_file.EnvVars(
            bazel_wrapper_dir=paths.wrapper_dir,
            session_bazelrc=session_bazelrc,
            session_dir=setup.session_dir,
            proxy_port=setup.proxy_port,
            supervisor_port=setup.supervisor_port,
            combined_ca=setup.combined_ca_path,
            bazelisk_path=setup.bazelisk_path,
            docker_env=setup.docker_env,
            hook_timestamp=hook_timestamp,
            mkcert_cert=setup.mkcert_cert,
            mkcert_key=setup.mkcert_key,
            secrets_env_vars=setup.secrets_env_vars,
            with_direnv=setup.with_direnv,
        )
        env_file.write_env_file(ctx.env_file_path, env_vars)
    logger.info("Wrote environment to %s", ctx.env_file_path)

    # Fire-and-forget Bazel server warmup (both web and CLI modes).
    # Store task reference in background_tasks to prevent GC before completion.
    if settings.warmup_bazel_server and background_tasks is not None:
        task = asyncio.create_task(
            bazel_server_warmup.warmup_bazel_server(
                wrapper_path=paths.wrapper_path, project_dir=ctx.project_dir, env_file=ctx.env_file_path
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # Build structured session context for Claude Code transcript
    with tracer.start_as_current_span("emit_session_context", context=root_ctx):
        status = "ERRORS" if collector.has_errors else "OK with warnings" if collector.has_warnings else "OK"
        extra_context = _render_extra_context(project_dir, setup.secrets, setup.fork_result)
        template = Template((_TEMPLATES_DIR / "session_context.mako").read_text())
        context_output: str = template.render(
            WARNING=logging.WARNING,
            build_commit=get_build_info().commit,
            status=status,
            proxy=setup.auth_proxy,
            container=setup.container,
            precommit=setup.precommit,
            PrecommitInstallingHooks=precommit_setup.PrecommitInstallingHooks,
            mkcert=setup.mkcert,
            log_entries=collector.buffer,
            secrets=setup.secrets,
            extra_context=extra_context,
            log_file=log_file,
            buildbuddy_configured=setup.buildbuddy_configured,
        )
        output = SessionStartOutput(
            hook_specific_output=SessionStartHookSpecificOutput(additional_context=context_output.rstrip("\n"))
        )

    root_span.end()
    return output


async def handle(
    hook_input: SessionStartHookInput,
    paths: SessionPaths,
    settings: HookSettings,
    caller_env: dict[str, str],
    http: httpx.Client,
    otlp_exporter: DeferredOtlpExporter,
    proxy: AuthForwardingProxy | None,
    background_tasks: set[asyncio.Task[object]] | None = None,
) -> SessionStartOutput:
    """Entry point called from the hook daemon with the client's env."""
    logger.info("Caller environment: %s", caller_env)
    ctx = CallerContext.from_env(caller_env)
    return await run_session(
        hook_input, paths, settings, ctx, http, otlp_exporter, proxy=proxy, background_tasks=background_tasks
    )
