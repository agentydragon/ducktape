"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy and git hooks.
CLI mode: Sets up per-session bazel wrapper with direnv integration.

Both modes render a per-session bazelrc from the unified bazelrc.mako template
and install a bazel wrapper that injects --bazelrc=<session-bazelrc>.
"""

import asyncio
import logging
import logging.handlers
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anyio
import httpx
import requests
from mako.template import Template
from opentelemetry import trace

from devinfra.claude import env_file
from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.auth_proxy.proxy import AuthForwardingProxy
from devinfra.claude.auth_proxy.vars import get_proxy_url
from devinfra.claude.claude_api.hooks.session_start import (
    SessionStartHookInput,
    SessionStartHookSpecificOutput,
    SessionStartOutput,
)
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.errors import SkipError
from devinfra.claude.hook_config import HOOKS_DOTDIR, HookConfig, OtelConfig, SecretSource
from devinfra.claude.sops_decrypt import load_age_identities

# isort: off
# Bazel subpackage imports must use `from pkg import module` form (not
# `from pkg.module import symbol`) due to auto-generated __init__.py stubs.
# isort would merge these into the block above, breaking the pattern.
from devinfra.claude.hook_daemon.session_start import apt
from devinfra.claude.hook_daemon.session_start import bazel_warmup
from devinfra.claude.hook_daemon.session_start import bazelisk
from devinfra.claude.hook_daemon.session_start import buildbuddy

from devinfra.claude.hook_daemon.session_start import container_runtime
from devinfra.claude.hook_daemon.session_start import fork_remote
from devinfra.claude.hook_daemon.session_start import mkcert
from devinfra.claude.hook_daemon.session_start import platform_detect
from devinfra.claude.hook_daemon.session_start import precommit
from devinfra.claude.hook_daemon.session_start import secret_sources
from devinfra.claude.hook_daemon.session_start import tmpfs
from devinfra.claude.hook_daemon.session_start import tune_rootfs

# isort: on
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter
from devinfra.claude.managed_files import write_config
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import CONFIG_FILES, HookSettings, ProxyMode
from devinfra.claude.supervisor import setup as supervisor_setup

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"


def _build_secrets_env_vars(secrets: secret_sources.SecretsResult | None) -> dict[str, str] | None:
    """Build env var dict from SecretsResult for the session env file."""
    if not secrets:
        return None
    env_vars: dict[str, str] = {}
    if secrets.buildbuddy_api_key:
        env_vars["BUILDBUDDY_API_KEY"] = secrets.buildbuddy_api_key
    if secrets.github_token:
        env_vars["GITHUB_TOKEN"] = secrets.github_token
    if secrets.kubeconfig_path:
        env_vars["KUBECONFIG"] = str(secrets.kubeconfig_path)
    return env_vars or None


@dataclass(frozen=True)
class CallerContext:
    """Structured env vars extracted from the hook client's environment.

    Replaces passing raw dict[str, str] through the call stack. Extracted once
    at the session start entry point (handle), then threaded through.
    """

    env_file_path: Path
    web_mode: bool
    project_dir: Path
    caller_env: dict[str, str]

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
            caller_env=env,
        )


# ============================================================================
# Platform setup result
# ============================================================================


@dataclass
class PlatformSetup:
    """Results of platform-specific setup (web or CLI).

    Carries actual setup results — not copies of fields derivable from
    paths, settings, or the result objects themselves.
    """

    # Platform-specific results
    platform: platform_detect.PlatformInfo
    auth_proxy: proxy_setup.ProxySetup | None = None
    container: container_runtime.ContainerRuntimeSetup | None = None
    precommit_result: precommit.PrecommitSetup | None = None
    mkcert_result: mkcert.MkcertSetup | None = None
    bazelisk_path: Path | None = None
    docker_env: dict[str, str] | None = None
    bazel_cache_dir: Path | None = None
    with_direnv: bool = False

    # Shared-step results (populated by run_session, not platform setup)
    secrets: secret_sources.SecretsResult | None = None
    fork_result: fork_remote.ForkRemoteSetup | None = None
    buildbuddy_configured: bool = False


# ============================================================================
# Shared helpers
# ============================================================================


def _render_extra_context(
    project_dir: Path,
    secrets: secret_sources.SecretsResult | None,
    fork_result: fork_remote.ForkRemoteSetup | None = None,
    *,
    web_mode: bool = False,
) -> str:
    """Render repo-specific context from .claude_hooks/templates/context.mako if it exists."""
    extra_template_path = project_dir / HOOKS_DOTDIR / "templates" / "context.mako"
    if not extra_template_path.exists():
        return ""
    template = Template(extra_template_path.read_text())
    result: str = template.render(secrets=secrets, fork_result=fork_result, web_mode=web_mode)
    return result.rstrip("\n")


def _build_otlp_session(proxy_url: str | None, ca_path: Path | None) -> requests.Session:
    """Build a requests.Session for OTLP.

    requests derives Proxy-Authorization from embedded proxy URL credentials automatically,
    unlike raw urllib3 which requires explicit headers on HTTPS CONNECT tunnels.
    """
    session = requests.Session()
    if proxy_url:
        session.proxies = {"https": proxy_url, "http": proxy_url}
    if ca_path and ca_path.exists():
        session.verify = str(ca_path)
    return session


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
    proxy: AuthForwardingProxy | None,
    platform: platform_detect.PlatformInfo,
) -> PlatformSetup:
    """Web mode: supervisor, proxy, containers, parallel installs.

    Returns a PlatformSetup with platform-specific results. K8s secrets,
    BuildBuddy, and fork remote are handled in the unified run_session() path.

    Platform drives tmpfs, Docker storage driver, and JVM heap decisions
    (see platform_detect.py, container_spec.md).
    """
    logger.info("Setting up dev environment...")

    async def traced_supervisor_start():
        with tracer.start_as_current_span("supervisor_start", context=root_ctx):
            return await supervisor_setup.start(paths, settings)

    # Start supervisor (required by proxy and docker)
    supervisor_task = asyncio.create_task(traced_supervisor_start())

    async def mount_tmpfs_at(path: Path) -> bool:
        """Mount a tmpfs at the given path. Returns True on success, False on failure.

        On Firecracker (ext4 root), tmpfs is unnecessary — disk I/O is
        adequate (seq write 98 MB/s, seq read 241 MB/s, 4K write 92 MB/s)
        and tmpfs would waste RAM. See container_spec.md IO benchmarks.
        """
        await anyio.Path(path).mkdir(parents=True, exist_ok=True)
        if not platform.needs_tmpfs_for_io:
            logger.info("Skipping tmpfs mount at %s (root_fstype=%s supports fast I/O)", path, platform.root_fstype)
            return False
        try:
            await run_in_thread(tmpfs.ensure_tmpfs_mounted, path)
            return True
        except Exception as e:
            logger.warning("tmpfs mount failed at %s, will fall back to 9p: %s", path, e)
            return False

    async def setup_proxy_credentials() -> proxy_setup.ProxySetup:
        """Write proxy credentials and set up CA/truststore (proxy already running in-process)."""
        with tracer.start_as_current_span("setup_proxy", context=root_ctx):
            return await proxy_setup.setup_auth_proxy(paths, settings, proxy=proxy)

    async def setup_container_runtime_task() -> container_runtime.ContainerRuntimeSetup:
        """Set up Docker (depends on supervisor).

        Storage driver selection follows from platform detection:
        - Firecracker (ext4): overlay works natively, skip tmpfs
        - gVisor (9p): mount tmpfs first, then overlay on tmpfs
        - gVisor without tmpfs: fall back to vfs
        """
        with tracer.start_as_current_span("setup_container_runtime", context=root_ctx):
            storage_dir = container_runtime.get_storage_dir(paths, settings)
            if storage_dir is None:
                raise SkipError("Docker setup disabled (setup_docker=False)")
            supervisor_result = await supervisor_task
            tmpfs_mounted = await mount_tmpfs_at(storage_dir)
            return await container_runtime.setup_container_runtime(
                paths,
                settings,
                supervisor_result.client,
                tmpfs_mounted=tmpfs_mounted,
                root_supports_overlay=platform.root_supports_overlay,
            )

    async def setup_bazel_on_tmpfs() -> tmpfs.TmpfsSetup:
        """Set up Bazel cache (mounts dedicated tmpfs under session dir)."""
        with tracer.start_as_current_span("setup_bazel_tmpfs", context=root_ctx):
            bazel_cache_dir = paths.bazel_cache_dir
            await mount_tmpfs_at(bazel_cache_dir)
            return tmpfs.setup_bazel_cache(bazel_cache_dir)

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    # Dependency graph:
    #   apt_task (no deps — runs immediately)
    #   proxy_task (in-process, no supervisor dependency)
    #   supervisor_task ── setup_container_runtime (Docker)
    #                      (mounts its own tmpfs internally)
    #   setup_bazel_on_tmpfs mounts its own tmpfs independently
    logger.info("Starting parallel installations...")
    # Resolve bazelisk path early (fast shutil.which — wrapper installed later in run_session).
    bazelisk_path = bazelisk.resolve_bazelisk()

    apt_packages: list[str] = []
    if settings.install_apt_packages:
        apt_packages.extend(apt.NATIVE_DEV_PACKAGES)
    else:
        logger.info("Skipping native apt packages (install_apt_packages=False)")

    @tracer.start_as_current_span("install_apt_packages", context=root_ctx)
    async def traced_apt():
        return await apt.install_packages(apt_packages)

    apt_task = asyncio.create_task(traced_apt())

    # Proxy task starts without BuildBuddy state (buildbuddy setup depends on
    # k8s secrets which in turn depend on proxy being up for TLS).
    # Proxy runs in-process (daemon threads, started by hook daemon server).
    # This task writes credentials and sets up CA/truststore.
    proxy_task = asyncio.create_task(setup_proxy_credentials())

    async def mkcert_generate_certs() -> mkcert.MkcertSetup:
        """Generate mkcert certs (no proxy dependency — runs immediately in parallel)."""
        with tracer.start_as_current_span("setup_mkcert", context=root_ctx):
            if not settings.install_mkcert:
                raise SkipError("mkcert disabled (install_mkcert=False)")
            # Pass combined_ca=None: bundle append happens in mkcert_append_bundle
            return await mkcert.setup_mkcert(paths, combined_ca=None)

    # Start cert generation immediately, without waiting for the proxy.
    mkcert_task = asyncio.create_task(mkcert_generate_certs())

    async def mkcert_append_bundle() -> mkcert.MkcertSetup:
        """Append mkcert CA to the combined CA bundle (depends on proxy + cert gen)."""
        with tracer.start_as_current_span("mkcert_append_bundle", context=root_ctx):
            mkcert_result = await mkcert_task
            await proxy_task
            combined_ca = paths.auth_proxy_combined_ca
            if combined_ca.exists():
                mkcert.append_mkcert_ca_to_bundle(mkcert_result.ca_root, combined_ca)
            return mkcert_result

    @tracer.start_as_current_span("install_precommit", context=root_ctx)
    async def traced_precommit():
        return await run_in_thread(precommit.install_precommit, project_dir)

    results = await asyncio.gather(
        proxy_task,
        setup_container_runtime_task(),
        traced_precommit(),
        setup_bazel_on_tmpfs(),
        mkcert_append_bundle(),
        apt_task,
        run_in_thread(tune_rootfs.reduce_reserved_blocks),
        return_exceptions=True,
    )
    # Unpack with explicit type annotations for mypy
    auth_proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    container_result: container_runtime.ContainerRuntimeSetup | BaseException = results[1]
    precommit_result: precommit.PrecommitSetup | BaseException = results[2]
    tmpfs_result: tmpfs.TmpfsSetup | BaseException = results[3]
    mkcert_result: mkcert.MkcertSetup | BaseException = results[4]
    apt_result: apt.AptSetup | BaseException = results[5]
    tune_result: None | BaseException = results[6]

    # Log non-critical failures
    if isinstance(tune_result, BaseException):
        logger.warning("Failed to reduce reserved blocks: %s", tune_result)
    if isinstance(precommit_result, BaseException):
        logger.warning("Failed to install git pre-commit: %s", precommit_result)
    if isinstance(tmpfs_result, BaseException):
        logger.warning("Failed to set up tmpfs caches: %s", tmpfs_result)
    if isinstance(mkcert_result, SkipError):
        logger.info("mkcert setup skipped: %s", mkcert_result)
    elif isinstance(mkcert_result, BaseException):
        logger.warning("Failed to set up mkcert: %s", mkcert_result)
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

    logger.info(
        "Ready: bazel=%s, proxy=%s, CA=%s", bazelisk_path, auth_proxy_result.status, auth_proxy_result.ca_status
    )
    logger.info("Container: %s", container_result)

    return PlatformSetup(
        platform=platform,
        auth_proxy=auth_proxy_result,
        container=None if isinstance(container_result, BaseException) else container_result,
        precommit_result=None if isinstance(precommit_result, BaseException) else precommit_result,
        mkcert_result=None if isinstance(mkcert_result, BaseException) else mkcert_result,
        bazelisk_path=bazelisk_path,
        docker_env=docker_env,
        bazel_cache_dir=tmpfs_result.bazel_cache if isinstance(tmpfs_result, tmpfs.TmpfsSetup) else None,
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

    # Detect platform early (safe in both modes — reads /proc + psutil).
    platform = platform_detect.detect()

    # Platform-specific setup (proxy, containers, certs, etc.)
    if ctx.web_mode:
        setup = await _setup_web(paths, settings, project_dir, tracer, root_ctx, proxy=proxy, platform=platform)
    else:
        setup = PlatformSetup(platform=platform, with_direnv=True)

    # -- Shared steps: secrets, BuildBuddy, fork remote --
    combined_ca = setup.auth_proxy.combined_ca if setup.auth_proxy else None
    proxy_url = (setup.auth_proxy.proxy_url if setup.auth_proxy else None) or get_proxy_url(ctx.caller_env)

    # Resolve secrets from tagged-union config (each field resolved independently).
    with tracer.start_as_current_span("resolve_secrets", context=root_ctx):
        secrets_cfg = hook_config.secrets if hook_config else None
        age_identities = load_age_identities(settings.age_key) if settings.age_key else None
        k8s_api = None
        k8s_namespace = hook_config.k8s.namespace if hook_config and hook_config.k8s else None
        if settings.k8s_token and hook_config and hook_config.k8s:
            try:
                k8s_api = secret_sources.setup_k8s_client(
                    token=settings.k8s_token, k8s_cfg=hook_config.k8s, combined_ca_path=combined_ca, proxy=proxy_url
                )
            except Exception as e:
                logger.warning("K8s client setup failed: %s", e)

        def resolve(source: SecretSource) -> str | None:
            return secret_sources.resolve_secret(
                source,
                project_dir=project_dir,
                age_identities=age_identities,
                k8s_api=k8s_api,
                k8s_namespace=k8s_namespace,
            )

        setup.secrets = secret_sources.SecretsResult()
        if secrets_cfg:
            if secrets_cfg.buildbuddy_api_key:
                setup.secrets.buildbuddy_api_key = resolve(secrets_cfg.buildbuddy_api_key)
            if secrets_cfg.github_token:
                setup.secrets.github_token = resolve(secrets_cfg.github_token)
            if secrets_cfg.otel_bearer_token:
                setup.secrets.otel_bearer_token = resolve(secrets_cfg.otel_bearer_token)

        # Write kubeconfig when k8s client is available.
        if k8s_api and settings.k8s_token and hook_config and hook_config.k8s:
            setup.secrets.kubeconfig_path = secret_sources.write_kubeconfig(
                token=settings.k8s_token,
                k8s_cfg=hook_config.k8s,
                session_dir=paths.session_dir,
                combined_ca_path=combined_ca,
                proxy_url=proxy_url,
            )

    # Configure BuildBuddy now that secrets are available.
    buildbuddy_api_key = setup.secrets.buildbuddy_api_key
    with tracer.start_as_current_span("setup_buildbuddy", context=root_ctx):
        if ctx.web_mode or buildbuddy_api_key:
            buildbuddy_result = await run_in_thread(lambda: buildbuddy.setup_buildbuddy(api_key=buildbuddy_api_key))
            setup.buildbuddy_configured = (
                isinstance(buildbuddy_result, buildbuddy.BuildbuddySetup) and buildbuddy_result.configured
            )
            if isinstance(buildbuddy_result, BaseException):
                logger.warning("Failed to configure BuildBuddy: %s", buildbuddy_result)
        else:
            setup.buildbuddy_configured = buildbuddy.is_buildbuddy_configured()

    # Ensure 'fork' git remote when GITHUB_TOKEN is available.
    if setup.secrets.github_token:
        try:
            with tracer.start_as_current_span("setup_fork_remote", context=root_ctx):
                setup.fork_result = fork_remote.ensure_fork_remote(setup.secrets.github_token, project_dir)
        except Exception as e:
            logger.warning("Fork remote setup failed: %s", e)

    # Configure OTLP now that secrets (with bearer token) are available.
    # Bearer token overrides config file / env var. Idempotent across sessions.
    if hook_config and hook_config.otel:
        otel_config = hook_config.otel.with_env_overrides()
        otel_token = setup.secrets.otel_bearer_token if setup.secrets else None
        if otel_token:
            otel_config = OtelConfig(endpoint=otel_config.endpoint, bearer_token=otel_token)
        otlp_session = _build_otlp_session(proxy_url, combined_ca)
        otlp_exporter.configure(otel_config, session=otlp_session)

    # Render session bazelrc
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        bazelrc_template = Template(
            CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"]
        )
        bazelrc_content: str = bazelrc_template.render(
            web_proxy=ctx.web_mode,
            use_tcp_proxy=settings.proxy_mode == ProxyMode.TCP,
            proxy_port=setup.auth_proxy.port if setup.auth_proxy else None,
            remote_proxy_sock=paths.remote_proxy_sock,
            truststore_path=paths.auth_proxy_truststore,
            truststore_password=proxy_setup.TRUSTSTORE_PASSWORD,
            combined_ca_path=combined_ca,
            buildbuddy_configured=setup.buildbuddy_configured,
            buildbuddy_bazelrc=buildbuddy.BUILDBUDDY_BAZELRC,
            bazel_cache_dir=setup.bazel_cache_dir,
            platform=setup.platform,
        )
        session_bazelrc = paths.session_dir / "bazelrc"
        write_config(session_bazelrc, bazelrc_content, "session bazelrc")

    # Install bazel wrapper (single canonical install for both web and CLI modes).
    with tracer.start_as_current_span("install_bazel_wrappers", context=root_ctx):
        bazelisk.install_wrapper(paths)

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
            session_dir=paths.session_dir,
            proxy_port=setup.auth_proxy.port if setup.auth_proxy else None,
            supervisor_port=settings.supervisor_port,
            combined_ca=combined_ca,
            bazelisk_path=setup.bazelisk_path,
            docker_env=setup.docker_env,
            hook_timestamp=hook_timestamp,
            mkcert_cert=setup.mkcert_result.cert_path if setup.mkcert_result else None,
            mkcert_key=setup.mkcert_result.key_path if setup.mkcert_result else None,
            secrets_env_vars=_build_secrets_env_vars(setup.secrets),
            with_direnv=setup.with_direnv,
            extra_env_script=hook_config.extra_env_script if hook_config else None,
        )
        env_file.write_env_file(ctx.env_file_path, env_vars)
    logger.info("Wrote environment to %s", ctx.env_file_path)

    # Fire-and-forget Bazel server warmup (both web and CLI modes).
    # Store task reference in background_tasks to prevent GC before completion.
    if settings.warmup_bazel_server and background_tasks is not None:
        task = asyncio.create_task(
            bazel_warmup.warmup_bazel_server(
                wrapper_path=paths.wrapper_path, project_dir=ctx.project_dir, env_file=ctx.env_file_path
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    # Build structured session context for Claude Code transcript
    with tracer.start_as_current_span("emit_session_context", context=root_ctx):
        status = "ERRORS" if collector.has_errors else "OK with warnings" if collector.has_warnings else "OK"
        extra_context = _render_extra_context(project_dir, setup.secrets, setup.fork_result, web_mode=ctx.web_mode)
        template = Template((_TEMPLATES_DIR / "session_context.mako").read_text())
        context_output: str = template.render(
            WARNING=logging.WARNING,
            status=status,
            proxy=setup.auth_proxy,
            container=setup.container,
            precommit=setup.precommit_result,
            PrecommitInstallingHooks=precommit.PrecommitInstallingHooks,
            PrecommitNotInstalled=precommit.PrecommitNotInstalled,
            mkcert=setup.mkcert_result,
            log_entries=collector.buffer,
            secrets=setup.secrets,
            extra_context=extra_context,
            log_file=log_file,
            buildbuddy_configured=setup.buildbuddy_configured,
            platform=setup.platform,
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
