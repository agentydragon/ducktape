"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy and git hooks.
CLI mode: Sets up per-session bazel wrapper with direnv integration.

Both modes render a per-session bazelrc from the unified bazelrc.mako template
and install a bazel wrapper that injects --bazelrc=<session-bazelrc>.
"""

import asyncio
import logging
import logging.handlers
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mako.template import Template
from opentelemetry import trace

from devinfra.build_info import get_build_info
from devinfra.claude import (
    bazelisk_setup,
    buildbuddy_setup,
    cli_tools_setup,
    container_runtime,
    env_file,
    fork_remote_setup,
    k8s_secrets_setup,
    mkcert_setup,
    nix_setup,
    precommit_setup,
    tmpfs_setup,
)
from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.claude_api.hooks.session_start import (
    SessionStartHookInput,
    SessionStartHookSpecificOutput,
    SessionStartOutput,
)
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.errors import SkipError
from devinfra.claude.hook_config import HOOKS_DOTDIR, HookConfig
from devinfra.claude.managed_files import write_config
from devinfra.claude.settings import CONFIG_FILES, HookSettings, is_web_mode
from devinfra.claude.supervisor import setup as supervisor_setup
from devinfra.claude.tracing import init_tracing, shutdown_tracing
from util import env

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


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
    repo_root: Path | None = None
    bazelisk_path: Path | None = None
    nix_paths: list[Path] = field(default_factory=list)
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
    """Configure root logger so all modules in devinfra.claude get handlers.

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
# Platform-specific setup
# ============================================================================


async def _setup_web(
    settings: HookSettings,
    project_dir: Path,
    tracer: trace.Tracer,
    root_ctx: trace.Context,
    hook_config: k8s_secrets_setup.HookConfig | None,
) -> PlatformSetup:
    """Web mode: supervisor, proxy, containers, secrets, parallel installs.

    Returns a fully populated PlatformSetup with all results needed by the
    shared downstream steps.
    """
    logger.info("Setting up dev environment...")

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
        """Install bazelisk and wrapper.

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

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    # Dependency graph:
    #   supervisor_task ──┬── proxy_task ──── setup_mkcert_with_proxy
    #                     └── setup_container_runtime (Docker or Podman)
    #                         (each runtime mounts its own tmpfs internally)
    #   setup_bazel_on_tmpfs mounts its own tmpfs independently
    logger.info("Starting parallel installations...")

    # Proxy task starts without BuildBuddy state (buildbuddy setup depends on
    # k8s secrets which in turn depend on proxy being up for TLS).
    proxy_task = asyncio.create_task(setup_proxy_with_supervisor())

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

    @tracer.start_as_current_span("install_precommit", context=root_ctx)
    async def traced_precommit():
        return await run_in_thread(precommit_setup.install_precommit, project_dir, settings.session_dir)

    @tracer.start_as_current_span("install_nix", context=root_ctx)
    async def traced_nix():
        return await run_in_thread(nix_setup.install_nix, settings)

    @tracer.start_as_current_span("install_cli_tools", context=root_ctx)
    async def traced_cli_tools():
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
    auth_proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    container_result: container_runtime.ContainerRuntimeSetup | BaseException = results[1]
    precommit_result: precommit_setup.PrecommitSetup | BaseException = results[2]
    nix_result: nix_setup.NixSetup | BaseException = results[3]
    bazelisk_result: bazelisk_setup.BazeliskSetup | BaseException = results[4]
    tmpfs_result: tmpfs_setup.TmpfsSetup | BaseException = results[5]
    mkcert_result: mkcert_setup.MkcertSetup | BaseException = results[6]
    cli_tools_result: list[str] | BaseException = results[7]

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

    # Handle nix result
    if isinstance(nix_result, SkipError):
        logger.info("Nix setup skipped: %s", nix_result)
    elif isinstance(nix_result, BaseException):
        logger.warning("Failed to install nix: %s", nix_result)

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

    # Verify combined CA was created (sanity check - should always exist after successful proxy setup)
    combined_ca = settings.get_auth_proxy_combined_ca()
    if not combined_ca.exists():
        raise RuntimeError("Combined CA bundle not found - proxy setup incomplete")

    # Read k8s secrets now that combined CA is available for TLS.
    # Route through the auth proxy so the upstream egress proxy gets credentials.
    secrets: k8s_secrets_setup.K8sSecretsResult | None = None
    if settings.k8s_token and hook_config:
        with tracer.start_as_current_span("setup_k8s_secrets", context=root_ctx):
            secrets = k8s_secrets_setup.setup_k8s_secrets(
                token=settings.k8s_token,
                session_dir=settings.session_dir,
                combined_ca_path=combined_ca,
                config=hook_config,
                proxy=f"http://localhost:{settings.get_auth_proxy_port()}",
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
    if isinstance(bazelisk_result, bazelisk_setup.BazeliskSetup) and bazelisk_result.bazelisk_skipped:
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

    logger.info(
        "Ready: bazel=%s, proxy=%s, CA=%s", bazelisk_result, auth_proxy_result.status, auth_proxy_result.ca_status
    )
    logger.info("Nix: %s", nix_setup.find_nix_bin())
    logger.info("Container: %s", container_result)

    return PlatformSetup(
        # Bazelrc rendering
        proxy_port=settings.get_auth_proxy_port(),
        truststore_path=settings.get_auth_proxy_truststore(),
        truststore_password=proxy_setup.TRUSTSTORE_PASSWORD,
        local_proxy=f"http://localhost:{settings.get_auth_proxy_port()}",
        combined_ca_path=combined_ca,
        bazel_cache_dir=tmpfs_result.bazel_cache if isinstance(tmpfs_result, tmpfs_setup.TmpfsSetup) else None,
        # EnvVars
        session_dir=settings.session_dir,
        supervisor_port=settings.get_supervisor_port(),
        repo_root=project_dir,
        bazelisk_path=bazelisk_path,
        nix_paths=nix_result.paths if isinstance(nix_result, nix_setup.NixSetup) else [],
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
    hook_input: SessionStartHookInput, settings: HookSettings, env_file_path: Path, *, web_mode: bool
) -> SessionStartOutput:
    """Unified session setup for both web and CLI modes.

    Dispatches platform-specific setup, then runs shared steps:
    bazelrc render, wrapper install, env file write, session context emit.
    """
    collector, log_file = setup_logging(settings, print_banner=web_mode)
    tracer, trace_file = init_tracing(hook_input.session_id, settings.session_dir)
    mode_label = "web" if web_mode else "cli"
    root_span = tracer.start_span(
        "session_start",
        attributes={"session.id": hook_input.session_id, "hook.source": hook_input.source, "mode": mode_label},
    )
    root_ctx = trace.set_span_in_context(root_span)

    logger.info("Session start hook (%s mode)", mode_label)
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")
    logger.info("Environment: %s", dict(os.environ))

    project_dir = env.get_required_env_path("CLAUDE_PROJECT_DIR")
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)
    logger.info("Session directory: %s", settings.session_dir)

    # Load hook config (general config file, not gated on k8s_token).
    hook_config = HookConfig.load_from_repo(project_dir)

    # K8s secrets are read after platform setup (proxy must be up for web mode TLS).
    secrets: k8s_secrets_setup.K8sSecretsResult | None = None

    # Platform-specific setup
    if web_mode:
        setup = await _setup_web(settings, project_dir, tracer, root_ctx, hook_config)
    else:
        # CLI mode: read k8s secrets (no proxy needed, combined_ca_path=None).
        if settings.k8s_token and hook_config:
            secrets = k8s_secrets_setup.setup_k8s_secrets(
                token=settings.k8s_token, session_dir=settings.session_dir, combined_ca_path=None, config=hook_config
            )
        setup = PlatformSetup(
            buildbuddy_configured=buildbuddy_setup.is_buildbuddy_configured(),
            with_direnv=True,
            secrets=secrets,
            secrets_env_vars=secrets.env_vars if secrets else None,
        )

    # Render session bazelrc
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        bazelrc_template = Template(
            CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"]
        )
        bazelrc_content: str = bazelrc_template.render(
            web_proxy=web_mode,
            proxy_port=setup.proxy_port,
            truststore_path=setup.truststore_path,
            truststore_password=setup.truststore_password,
            local_proxy=setup.local_proxy,
            combined_ca_path=setup.combined_ca_path,
            buildbuddy_configured=setup.buildbuddy_configured,
            buildbuddy_bazelrc=buildbuddy_setup.BUILDBUDDY_BAZELRC,
            bazel_cache_dir=setup.bazel_cache_dir,
        )
        session_bazelrc = settings.session_dir / "bazelrc"
        write_config(session_bazelrc, bazelrc_content, "session bazelrc")

    # Install bazel wrapper (web mode already downloaded bazelisk in parallel)
    with tracer.start_as_current_span("install_bazel_wrappers", context=root_ctx):
        bazelisk_setup.install_wrapper(settings)

    # Generate timestamp
    hook_timestamp = datetime.now()
    timestamp_file = settings.session_dir / "session-hook-last-run"
    timestamp_file.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Write environment file
    with tracer.start_as_current_span("write_env_file", context=root_ctx):
        env_vars = env_file.EnvVars(
            bazel_wrapper_dir=settings.get_wrapper_dir(),
            session_bazelrc=session_bazelrc,
            session_dir=setup.session_dir,
            proxy_port=setup.proxy_port,
            supervisor_port=setup.supervisor_port,
            repo_root=setup.repo_root,
            combined_ca=setup.combined_ca_path,
            bazelisk_path=setup.bazelisk_path,
            nix_paths=setup.nix_paths,
            docker_env=setup.docker_env,
            hook_timestamp=hook_timestamp,
            mkcert_cert=setup.mkcert_cert,
            mkcert_key=setup.mkcert_key,
            secrets_env_vars=setup.secrets_env_vars,
            with_direnv=setup.with_direnv,
        )
        env_file.write_env_file(env_file_path, env_vars)
    logger.info("Wrote environment to %s", env_file_path)

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
    shutdown_tracing()
    logger.info("Trace file: %s", trace_file)
    return output


async def _async_handle(hook_input: SessionStartHookInput, settings: HookSettings) -> SessionStartOutput:
    """Async entry point called from hook_dispatch. Dispatches to web or CLI mode."""
    env_file_path = env.get_required_env_path("CLAUDE_ENV_FILE")
    web_mode = is_web_mode()
    return await run_session(hook_input, settings, env_file_path, web_mode=web_mode)
