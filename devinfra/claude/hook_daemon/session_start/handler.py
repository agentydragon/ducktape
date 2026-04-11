"""Unified session start hook for Claude Code (web and CLI).

Web mode (CLAUDE_CODE_REMOTE=true): Sets up auth proxy, containers, and background tasks.
CLI mode: Sets up per-session bazel wrapper with direnv integration.

Both modes render a per-session bazelrc from the unified bazelrc.mako template
and install a bazel wrapper that injects --bazelrc=<session-bazelrc>.
"""

import asyncio
import logging
import logging.handlers
import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anyio
from mako.template import Template
from opentelemetry import trace

from devinfra.claude import env_file
from devinfra.claude.auth_proxy import setup as proxy_setup
from devinfra.claude.auth_proxy.vars import get_proxy_url
from devinfra.claude.claude_api.hooks.session_start import (
    SessionStartHookInput,
    SessionStartHookSpecificOutput,
    SessionStartOutput,
)
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.errors import SkipError
from devinfra.claude.hook_daemon import templates
from devinfra.claude.hook_daemon.config import BackgroundCommand, ProfileConfig
from devinfra.claude.hook_daemon.session import Session
from devinfra.claude.hook_daemon.session_start import (
    buildbuddy,
    container_runtime,
    fork_remote,
    kubeconfig,
    mkcert,
    platform_detect,
    tmpfs,
)
from devinfra.claude.hook_daemon.shim_install import install as install_shim
from devinfra.claude.managed_files import write_config
from devinfra.claude.settings import CONFIG_FILES, HookSettings
from devinfra.claude.supervisor import setup as supervisor_setup

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


@dataclass
class SecretsResult:
    """Secrets read from os.environ (populated by env scripts at daemon startup)."""

    k8s_token: str | None = None
    buildbuddy_api_key: str | None = None
    github_token: str | None = None
    kubeconfig_path: Path | None = None


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
    mkcert_result: mkcert.MkcertSetup | None = None
    docker_env: dict[str, str] | None = None
    bazel_cache_dir: Path | None = None
    with_direnv: bool = False

    # Shared-step results (populated by run_session, not platform setup)
    secrets: SecretsResult = field(default_factory=SecretsResult)
    fork_result: fork_remote.ForkRemoteSetup | None = None
    buildbuddy_setup: buildbuddy.BuildbuddySetup = field(default_factory=buildbuddy.BuildbuddyNotConfigured)


async def _run_background_command(
    session: Session, cmd: BackgroundCommand, sock_path: Path, env_file_path: Path | None, project_dir: Path
) -> None:
    """Run a background shell command with lifecycle messages to the session mailbox.

    Passes HOOK_DAEMON_SOCK so scripts can post additional messages via
    ``curl --unix-socket $HOOK_DAEMON_SOCK -X POST http://localhost/mailbox -d '{"message":"..."}'``.
    """
    session.post_message(f"Task [{cmd.name}] started.")
    try:
        env = dict(os.environ)
        env["HOOK_DAEMON_SOCK"] = str(sock_path)

        shell_cmd = cmd.command
        if cmd.after_env and env_file_path:
            shell_cmd = f"source {shlex.quote(str(env_file_path))} && {shell_cmd}"

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            shell_cmd,
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cmd.timeout)
        if proc.returncode != 0:
            output = (stderr or stdout or b"").decode(errors="replace")[-500:]
            logger.error("Background command %s failed (exit %d): %s", cmd.name, proc.returncode, output)
            session.post_message(f"Task [{cmd.name}] failed, see hook daemon logs for details.")
        else:
            session.post_message(f"Task [{cmd.name}] completed successfully.")
    except TimeoutError:
        logger.exception("Background command %s timed out after %ds", cmd.name, cmd.timeout)
        session.post_message(f"Task [{cmd.name}] failed, see hook daemon logs for details.")
    except Exception:
        logger.exception("Background command %s failed", cmd.name)
        session.post_message(f"Task [{cmd.name}] failed, see hook daemon logs for details.")


def _launch_background_commands(
    session: Session,
    commands: list[BackgroundCommand],
    *,
    sock_path: Path,
    env_file_path: Path | None,
    project_dir: Path,
) -> None:
    """Launch background commands as fire-and-forget asyncio tasks."""
    for cmd in commands:
        task = asyncio.create_task(_run_background_command(session, cmd, sock_path, env_file_path, project_dir))
        session.track(task)


async def _setup_web(
    session: Session,
    settings: HookSettings,
    profile: ProfileConfig,
    project_dir: Path,
    root_ctx: trace.Context,
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
            return await supervisor_setup.start(session.paths, settings)

    # Start supervisor (required by docker)
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
        """Set up CA/truststore for TLS-inspecting proxy."""
        with tracer.start_as_current_span("setup_proxy", context=root_ctx):
            return await proxy_setup.setup_auth_proxy(session.paths)

    async def setup_container_runtime_task() -> container_runtime.ContainerRuntimeSetup:
        """Set up Docker (depends on supervisor).

        Storage driver selection follows from platform detection:
        - Firecracker (ext4): overlay works natively, skip tmpfs
        - gVisor (9p): mount tmpfs first, then overlay on tmpfs
        - gVisor without tmpfs: fall back to vfs
        """
        with tracer.start_as_current_span("setup_container_runtime", context=root_ctx):
            if not profile.setup_docker:
                raise SkipError("Docker setup disabled (setup_docker=False)")
            storage_dir = session.paths.container_storage_dir
            supervisor_result = await supervisor_task
            tmpfs_mounted = await mount_tmpfs_at(storage_dir)
            return await container_runtime.setup_container_runtime(
                session.paths,
                supervisor_result,
                tmpfs_mounted=tmpfs_mounted,
                root_supports_overlay=platform.root_supports_overlay,
            )

    async def setup_bazel_on_tmpfs() -> tmpfs.TmpfsSetup:
        """Set up Bazel cache (mounts dedicated tmpfs under session dir)."""
        with tracer.start_as_current_span("setup_bazel_tmpfs", context=root_ctx):
            bazel_cache_dir = session.paths.bazel_cache_dir
            await mount_tmpfs_at(bazel_cache_dir)
            return tmpfs.setup_bazel_cache(bazel_cache_dir)

    bazel_tmpfs_task = asyncio.create_task(setup_bazel_on_tmpfs())

    # PARALLEL: All setup tasks (with explicit dependencies via task awaits)
    # Dependency graph:
    #   proxy_task (in-process, no supervisor dependency)
    #   supervisor_task ── setup_container_runtime (Docker)
    #                      (mounts its own tmpfs internally)
    #   setup_bazel_on_tmpfs mounts its own tmpfs independently
    #   immediate background_commands (no deps — run immediately)
    logger.info("Starting parallel installations...")

    # Fire-and-forget immediate background commands (notifications via session mailbox).
    immediate_cmds = [cmd for cmd in profile.background_commands if not cmd.after_env]
    _launch_background_commands(
        session, immediate_cmds, sock_path=session.paths.hook_daemon_sock, env_file_path=None, project_dir=project_dir
    )

    # Proxy task starts without BuildBuddy state (buildbuddy setup depends on
    # k8s secrets which in turn depend on proxy being up for TLS).
    # Proxy runs in-process (daemon threads, started by hook daemon server).
    # This task writes credentials and sets up CA/truststore.
    proxy_task = asyncio.create_task(setup_proxy_credentials())

    async def mkcert_generate_certs() -> mkcert.MkcertSetup:
        """Generate mkcert certs (no proxy dependency — runs immediately in parallel)."""
        with tracer.start_as_current_span("setup_mkcert", context=root_ctx):
            if not profile.install_mkcert:
                raise SkipError("mkcert disabled (install_mkcert=False)")
            # Pass combined_ca=None: bundle append happens in mkcert_append_bundle
            return await mkcert.setup_mkcert(session.paths, combined_ca=None)

    # Start cert generation immediately, without waiting for the proxy.
    mkcert_task = asyncio.create_task(mkcert_generate_certs())

    async def mkcert_append_bundle() -> mkcert.MkcertSetup:
        """Append mkcert CA to the combined CA bundle (depends on proxy + cert gen)."""
        with tracer.start_as_current_span("mkcert_append_bundle", context=root_ctx):
            mkcert_result = await mkcert_task
            await proxy_task
            combined_ca = session.paths.auth_proxy_combined_ca
            if combined_ca.exists():
                mkcert.append_mkcert_ca_to_bundle(mkcert_result.ca_root, combined_ca)
            return mkcert_result

    results = await asyncio.gather(
        proxy_task, setup_container_runtime_task(), bazel_tmpfs_task, mkcert_append_bundle(), return_exceptions=True
    )
    # Unpack with explicit type annotations for mypy
    auth_proxy_result: proxy_setup.ProxySetup | BaseException = results[0]
    container_result: container_runtime.ContainerRuntimeSetup | BaseException = results[1]
    tmpfs_result: tmpfs.TmpfsSetup | BaseException = results[2]
    mkcert_result: mkcert.MkcertSetup | BaseException = results[3]

    # Log non-critical failures
    if isinstance(tmpfs_result, BaseException):
        logger.warning("Failed to set up tmpfs caches: %s", tmpfs_result)
    if isinstance(mkcert_result, SkipError):
        logger.info("mkcert setup skipped: %s", mkcert_result)
    elif isinstance(mkcert_result, BaseException):
        logger.warning("Failed to set up mkcert: %s", mkcert_result)
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

    logger.info("Ready: proxy=%s, CA=%s", auth_proxy_result.status, auth_proxy_result.ca_status)
    logger.info("Container: %s", container_result)

    return PlatformSetup(
        platform=platform,
        auth_proxy=auth_proxy_result,
        container=None if isinstance(container_result, BaseException) else container_result,
        mkcert_result=None if isinstance(mkcert_result, BaseException) else mkcert_result,
        docker_env=docker_env,
        bazel_cache_dir=tmpfs_result.bazel_cache if isinstance(tmpfs_result, tmpfs.TmpfsSetup) else None,
    )


async def handle(
    session: Session,
    hook_input: SessionStartHookInput,
    settings: HookSettings,
    profile: ProfileConfig,
    ctx: CallerContext,
) -> SessionStartOutput:
    """Unified session setup for both web and CLI modes.

    Dispatches platform-specific setup, then runs shared steps:
    bazelrc render, wrapper install, env file write, session context emit.
    """

    collector = _setup_session_logging()
    log_file = session.paths.hook_daemon_log
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
    logger.info("Session directory: %s", session.paths.session_dir)

    # Detect platform early (safe in both modes — reads /proc + psutil).
    platform = platform_detect.detect()

    # Platform-specific setup (proxy, containers, certs, etc.)
    # Immediate background commands (after_env=False) are launched inside _setup_web
    # for web mode (to run in parallel with proxy/container setup). For CLI mode,
    # launch them here.
    if ctx.web_mode:
        setup = await _setup_web(session, settings, profile, project_dir, root_ctx, platform=platform)
    else:
        immediate_cmds = [cmd for cmd in profile.background_commands if not cmd.after_env]
        _launch_background_commands(
            session,
            immediate_cmds,
            sock_path=session.paths.hook_daemon_sock,
            env_file_path=None,
            project_dir=project_dir,
        )
        setup = PlatformSetup(platform=platform, with_direnv=True)

    # -- Shared steps: secrets, BuildBuddy, fork remote --
    combined_ca = setup.auth_proxy.combined_ca if setup.auth_proxy else None
    proxy_url = get_proxy_url(ctx.caller_env)

    # Read secrets from os.environ (populated by env scripts sourced at daemon startup).
    with tracer.start_as_current_span("read_secrets", context=root_ctx):
        setup.secrets.k8s_token = os.environ.get("K8S_TOKEN")
        setup.secrets.buildbuddy_api_key = os.environ.get("BUILDBUDDY_API_KEY")
        setup.secrets.github_token = os.environ.get("GITHUB_TOKEN")

        # Write kubeconfig when token is available and profile enables it.
        # CLI profile skips this — the user has their own ~/.kube/config.
        k8s_token = setup.secrets.k8s_token or settings.k8s_token
        if k8s_token and profile.k8s and profile.write_kubeconfig:
            setup.secrets.kubeconfig_path = kubeconfig.write_kubeconfig(
                token=k8s_token,
                k8s_cfg=profile.k8s,
                session_dir=session.paths.session_dir,
                combined_ca_path=combined_ca,
                proxy_url=proxy_url,
            )

    # Configure BuildBuddy now that secrets are available.
    with tracer.start_as_current_span("setup_buildbuddy", context=root_ctx):
        if buildbuddy_api_key := setup.secrets.buildbuddy_api_key:
            session.buildbuddy_api_key = buildbuddy_api_key
            buildbuddy_result = await run_in_thread(
                lambda: buildbuddy.setup_buildbuddy(api_key=buildbuddy_api_key, session_dir=session.paths.session_dir)
            )
            if isinstance(buildbuddy_result, BaseException):
                logger.warning("Failed to configure BuildBuddy: %s", buildbuddy_result)
            else:
                setup.buildbuddy_setup = buildbuddy_result

    # Ensure 'fork' git remote when GITHUB_TOKEN is available.
    if setup.secrets.github_token:
        try:
            with tracer.start_as_current_span("setup_fork_remote", context=root_ctx):
                setup.fork_result = fork_remote.ensure_fork_remote(setup.secrets.github_token, project_dir)
        except Exception as e:
            logger.warning("Fork remote setup failed: %s", e)

    # Render session bazelrc
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        bazelrc_template = Template(
            CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"]
        )
        bazelrc_content: str = bazelrc_template.render(
            web_proxy=ctx.web_mode,
            bazel_remote_proxy_sock=session.paths.bazel_remote_proxy_sock if session.uds_remote else None,
            bazel_bes_proxy_sock=session.paths.bazel_bes_proxy_sock if session.bes_interceptor else None,
            truststore_path=session.paths.auth_proxy_truststore,
            truststore_password=proxy_setup.TRUSTSTORE_PASSWORD,
            combined_ca_path=combined_ca,
            buildbuddy_bazelrc=setup.buildbuddy_setup.bazelrc_path
            if isinstance(setup.buildbuddy_setup, buildbuddy.BuildbuddyConfigured)
            else None,
            bazel_cache_dir=setup.bazel_cache_dir,
            platform=setup.platform,
        )
        session_bazelrc = session.paths.session_dir / "bazelrc"
        write_config(session_bazelrc, bazelrc_content, "session bazelrc")

    # Install PATH shims (bazelisk --bazelrc injection + git safety).
    with tracer.start_as_current_span("install_shims", context=root_ctx):
        install_shim("bazelisk", session.paths)
        install_shim("git", session.paths)

    # Generate timestamp
    hook_timestamp = datetime.now()
    timestamp_file = session.paths.session_dir / "session-hook-last-run"
    timestamp_file.write_text(f"{hook_timestamp.isoformat()}\n")
    logger.info("Session start hook timestamp: %s", hook_timestamp.isoformat())

    # Write environment file
    with tracer.start_as_current_span("write_env_file", context=root_ctx):
        extra_env = _build_extra_env_script(profile)
        env_vars = env_file.EnvVars(
            bazel_wrapper_dir=session.paths.wrapper_dir,
            session_bazelrc=session_bazelrc,
            session_dir=session.paths.session_dir,
            supervisor_port=settings.supervisor_port,
            combined_ca=combined_ca,
            docker_env=setup.docker_env,
            hook_timestamp=hook_timestamp,
            mkcert_cert=setup.mkcert_result.cert_path if setup.mkcert_result else None,
            mkcert_key=setup.mkcert_result.key_path if setup.mkcert_result else None,
            kubeconfig_path=setup.secrets.kubeconfig_path,
            with_direnv=setup.with_direnv,
            extra_env_script=extra_env,
        )
        env_file.write_env_file(ctx.env_file_path, env_vars)
    logger.info("Wrote environment to %s", ctx.env_file_path)

    # Fire-and-forget after_env background commands (both web and CLI modes).
    # ORDERING: these run after env file is written (they source it) and after
    # bazel tmpfs cache mount (in the gather above) — otherwise bazel writes to
    # the underlying fs, then the tmpfs mount shadows those files.
    deferred_cmds = [cmd for cmd in profile.background_commands if cmd.after_env]
    _launch_background_commands(
        session,
        deferred_cmds,
        sock_path=session.paths.hook_daemon_sock,
        env_file_path=ctx.env_file_path,
        project_dir=ctx.project_dir,
    )

    # Build structured session context for Claude Code transcript
    with tracer.start_as_current_span("emit_session_context", context=root_ctx):
        extra_context = _render_extra_context(
            project_dir,
            setup.secrets,
            setup.fork_result,
            web_mode=ctx.web_mode,
            profile=profile,
            bazel_remote_proxy_sock=session.paths.bazel_remote_proxy_sock if session.uds_remote else None,
            bazel_bes_proxy_sock=session.paths.bazel_bes_proxy_sock if session.bes_interceptor else None,
        )
        context_output: str = templates.session_context.render(
            collector=collector,
            proxy=setup.auth_proxy,
            container=setup.container,
            background_commands=profile.background_commands,
            mkcert=setup.mkcert_result,
            extra_context=extra_context,
            log_file=log_file,
            buildbuddy_configured=isinstance(setup.buildbuddy_setup, buildbuddy.BuildbuddyConfigured),
            platform=setup.platform,
            profile=profile,
            bazel_remote_proxy_sock=session.paths.bazel_remote_proxy_sock if session.uds_remote else None,
        )
        output = SessionStartOutput(
            hook_specific_output=SessionStartHookSpecificOutput(additional_context=context_output)
        )

    root_span.end()
    return output


def _build_extra_env_script(profile: ProfileConfig) -> str | None:
    """Build extra inline env content from profile's env_exports."""
    if profile.env_exports:
        return profile.env_exports.rstrip()
    return None


# ============================================================================
# Shared helpers
# ============================================================================


def _render_extra_context(
    project_dir: Path,
    secrets: SecretsResult | None,
    fork_result: fork_remote.ForkRemoteSetup | None = None,
    *,
    web_mode: bool = False,
    profile: ProfileConfig,
    bazel_remote_proxy_sock: Path | None,
    bazel_bes_proxy_sock: Path | None,
) -> str:
    """Render per-profile context template if configured."""
    if not profile.context_template:
        return ""
    extra_template_path = project_dir / profile.context_template
    if not extra_template_path.exists():
        return ""
    template = Template(extra_template_path.read_text())
    result: str = template.render(
        secrets=secrets,
        fork_result=fork_result,
        web_mode=web_mode,
        profile=profile,
        bazel_remote_proxy_sock=bazel_remote_proxy_sock,
        bazel_bes_proxy_sock=bazel_bes_proxy_sock,
    )
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
