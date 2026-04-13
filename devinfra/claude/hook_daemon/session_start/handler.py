"""Unified session start hook for Claude Code.

Profile-driven setup: auth proxy, containers, tmpfs, and background tasks are
controlled by flags in the active profile (see ProfileConfig in config.py).

All profiles render a per-session bazelrc from the unified bazelrc.mako template
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
from devinfra.claude.claude_api.hooks.output import HookOutput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput, SessionStartHookSpecificOutput
from devinfra.claude.debug import log_entrypoint_debug
from devinfra.claude.errors import SkipError
from devinfra.claude.hook_daemon import templates
from devinfra.claude.hook_daemon.config import BackgroundCommand, ProfileConfig
from devinfra.claude.hook_daemon.models import StartupResult
from devinfra.claude.hook_daemon.session import BgStream, Session, _feed_queue
from devinfra.claude.hook_daemon.session_start import (
    buildbuddy,
    container_runtime,
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


@dataclass(frozen=True)
class CallerContext:
    """Structured env vars extracted from the hook client's environment.

    Replaces passing raw dict[str, str] through the call stack. Extracted once
    at the session start entry point (handle), then threaded through.
    """

    caller_env: dict[str, str]

    @property
    def env_file_path(self) -> Path:
        return Path(self.caller_env["CLAUDE_ENV_FILE"])

    @property
    def project_dir(self) -> Path:
        return Path(self.caller_env["CLAUDE_PROJECT_DIR"])

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "CallerContext":
        if not env.get("CLAUDE_ENV_FILE"):
            raise KeyError("CLAUDE_ENV_FILE environment variable is required")
        if not env.get("CLAUDE_PROJECT_DIR"):
            raise KeyError("CLAUDE_PROJECT_DIR environment variable is required")
        return cls(caller_env=env)


# ============================================================================
# Platform setup result
# ============================================================================


@dataclass
class PlatformSetup:
    """Results of profile-driven platform setup.

    Carries actual setup results — not copies of fields derivable from
    paths, settings, or the result objects themselves.
    """

    # Platform-specific results
    platform: platform_detect.PlatformInfo
    env_overlay: dict[str, str]  # vars added/changed by startup_env_script (delta over os.environ)
    auth_proxy: proxy_setup.ProxySetup | None = None
    container: container_runtime.ContainerRuntimeSetup | None = None
    mkcert_result: mkcert.MkcertSetup | None = None
    docker_env: dict[str, str] | None = None
    bazel_cache_dir: Path | None = None
    kubeconfig_path: Path | None = None
    # Shared-step results (populated by handle(), not platform setup)
    buildbuddy_setup: buildbuddy.BuildbuddySetup = field(default_factory=buildbuddy.BuildbuddyNotConfigured)

    @property
    def k8s_token(self) -> str | None:
        return self.env_overlay.get("K8S_TOKEN") or os.environ.get("K8S_TOKEN")

    @property
    def buildbuddy_api_key(self) -> str | None:
        return self.env_overlay.get("BUILDBUDDY_API_KEY") or os.environ.get("BUILDBUDDY_API_KEY")

    @property
    def github_token(self) -> str | None:
        return self.env_overlay.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")


async def _watch_proc(session: Session, cmd: BackgroundCommand, proc: asyncio.subprocess.Process) -> None:
    """Wait for a background process; post a lifecycle message on completion or timeout."""
    try:
        returncode = await asyncio.wait_for(proc.wait(), timeout=cmd.timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        session.post_message(f"Task [{cmd.name}] timed out after {cmd.timeout}s.")
        return
    session.post_message(f"Task [{cmd.name}] exited {returncode}.")


async def _launch_background_command(
    session: Session,
    cmd: BackgroundCommand,
    sock_path: Path,
    env_file_path: Path | None,
    project_dir: Path,
    env_overlay: dict[str, str],
) -> None:
    """Start a background command; wire stdout/stderr queues and a lifecycle watcher.

    Passes HOOK_DAEMON_SOCK so scripts can post additional messages via
    ``curl --unix-socket $HOOK_DAEMON_SOCK -X POST http://localhost/mailbox -d '{"message":"..."}'``.
    Output accumulates in per-source queues and is flushed by drain_bg_output() on the
    next REPL hook — without waiting for the process to finish.
    """
    session.post_message(f"Task [{cmd.name}] started.")
    env = {**os.environ, **env_overlay, "HOOK_DAEMON_SOCK": str(sock_path)}

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

    stdout_q: asyncio.Queue[str] = asyncio.Queue()
    stderr_q: asyncio.Queue[str] = asyncio.Queue()
    session.add_bg_source(cmd.name, BgStream.STDOUT, stdout_q)
    session.add_bg_source(cmd.name, BgStream.STDERR, stderr_q)
    session.track(asyncio.create_task(_feed_queue(proc.stdout, stdout_q)))  # type: ignore[arg-type]
    session.track(asyncio.create_task(_feed_queue(proc.stderr, stderr_q)))  # type: ignore[arg-type]
    session.track(asyncio.create_task(_watch_proc(session, cmd, proc)))


def _launch_background_commands(
    session: Session,
    commands: list[BackgroundCommand],
    *,
    sock_path: Path,
    env_file_path: Path | None,
    project_dir: Path,
    env_overlay: dict[str, str],
) -> None:
    """Launch background commands as fire-and-forget asyncio tasks."""
    for cmd in commands:
        task = asyncio.create_task(
            _launch_background_command(session, cmd, sock_path, env_file_path, project_dir, env_overlay)
        )
        session.track(task)


async def _setup_platform_services(
    session: Session,
    settings: HookSettings,
    profile: ProfileConfig,
    project_dir: Path,
    root_ctx: trace.Context,
    platform: platform_detect.PlatformInfo,
    env_overlay: dict[str, str],
) -> PlatformSetup:
    """Profile-driven platform services: supervisor, proxy, containers, tmpfs, certs.

    env_overlay is the delta from startup_env_script (new/changed vars vs. os.environ).
    Callers access secrets as properties (k8s_token, etc.).
    BuildBuddy, fork remote, and kubeconfig write are handled in handle().

    Which services run is controlled by profile flags (setup_auth_proxy,
    setup_docker, setup_tmpfs, install_mkcert).
    """
    logger.info("Setting up platform services...")

    async def traced_supervisor_start():
        with tracer.start_as_current_span("supervisor_start", context=root_ctx):
            return await supervisor_setup.start(session.paths, settings)

    # Start supervisor early (required by Docker setup below).
    supervisor_task = asyncio.create_task(traced_supervisor_start()) if profile.setup_docker else None

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
            if not profile.setup_auth_proxy:
                raise SkipError("Auth proxy setup disabled (setup_auth_proxy=False)")
            return await proxy_setup.setup_auth_proxy(session.paths)

    async def setup_container_runtime_task() -> container_runtime.ContainerRuntimeSetup:
        """Set up Docker (depends on supervisor).

        Storage driver selection follows from platform detection:
        - Firecracker (ext4): overlay works natively, skip tmpfs
        - gVisor (9p): mount tmpfs first, then overlay on tmpfs
        - gVisor without tmpfs: fall back to vfs
        """
        with tracer.start_as_current_span("setup_container_runtime", context=root_ctx):
            if not profile.setup_docker or not supervisor_task:
                raise SkipError("Docker setup disabled (setup_docker=False)")
            storage_dir = session.paths.container_storage_dir
            supervisor_result = await supervisor_task
            tmpfs_mounted = await mount_tmpfs_at(storage_dir) if profile.setup_tmpfs else False
            return await container_runtime.setup_container_runtime(
                session.paths,
                supervisor_result,
                tmpfs_mounted=tmpfs_mounted,
                root_supports_overlay=platform.root_supports_overlay,
            )

    async def setup_bazel_on_tmpfs() -> tmpfs.TmpfsSetup:
        """Set up Bazel cache (mounts dedicated tmpfs under session dir)."""
        with tracer.start_as_current_span("setup_bazel_tmpfs", context=root_ctx):
            if not profile.setup_tmpfs:
                raise SkipError("tmpfs disabled (setup_tmpfs=False)")
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
        session,
        immediate_cmds,
        sock_path=session.paths.hook_daemon_sock,
        env_file_path=None,
        project_dir=project_dir,
        env_overlay=env_overlay,
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

    # Log non-critical failures / skips
    if isinstance(auth_proxy_result, SkipError):
        logger.info("Auth proxy setup skipped: %s", auth_proxy_result)
    elif isinstance(auth_proxy_result, BaseException):
        # Proxy was requested but failed — propagate
        logger.error("Proxy setup failed: %s", auth_proxy_result)
        raise RuntimeError(f"Proxy setup failed: {auth_proxy_result}") from auth_proxy_result

    if isinstance(tmpfs_result, SkipError):
        logger.info("tmpfs setup skipped: %s", tmpfs_result)
    elif isinstance(tmpfs_result, BaseException):
        logger.warning("Failed to set up tmpfs caches: %s", tmpfs_result)

    if isinstance(mkcert_result, SkipError):
        logger.info("mkcert setup skipped: %s", mkcert_result)
    elif isinstance(mkcert_result, BaseException):
        logger.warning("Failed to set up mkcert: %s", mkcert_result)

    docker_env: dict[str, str] | None = None
    if isinstance(container_result, SkipError):
        logger.info("Container runtime setup skipped: %s", container_result)
    elif isinstance(container_result, BaseException):
        logger.warning("Failed to configure container runtime: %s", container_result)
    else:
        docker_env = container_result.env_vars

    if isinstance(auth_proxy_result, proxy_setup.ProxySetup):
        logger.info("Ready: proxy=%s, CA=%s", auth_proxy_result.status, auth_proxy_result.ca_status)
    logger.info("Container: %s", container_result)

    return PlatformSetup(
        platform=platform,
        env_overlay=env_overlay,
        auth_proxy=auth_proxy_result if isinstance(auth_proxy_result, proxy_setup.ProxySetup) else None,
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
    startup: StartupResult,
) -> HookOutput:
    """Profile-driven session setup.

    Dispatches platform services (proxy, containers, tmpfs, certs) based on
    profile flags, then runs shared steps: bazelrc render, wrapper install,
    env file write, session context emit.
    """

    collector = _setup_session_logging()
    log_file = session.paths.hook_daemon_log
    root_span = tracer.start_span(
        "session_start", attributes={"session.id": hook_input.session_id, "hook.source": hook_input.source}
    )
    root_ctx = trace.set_span_in_context(root_span)

    logger.info("Session start hook")
    logger.info("Hook input: %s", hook_input.model_dump_json())
    log_entrypoint_debug("session_start")

    project_dir = ctx.project_dir
    logger.info("CLAUDE_PROJECT_DIR: %s", project_dir)
    logger.info("Session directory: %s", session.paths.session_dir)

    # Detect platform early (reads /proc + psutil, safe in all environments).
    platform = platform_detect.detect()

    # Platform services (proxy, containers, certs, tmpfs) are individually gated
    # by profile flags inside _setup_platform_services. Services whose flags are
    # false get skipped via SkipError.
    setup = await _setup_platform_services(
        session, settings, profile, project_dir, root_ctx, platform=platform, env_overlay=startup.env_overlay
    )

    # -- Shared steps: secrets, BuildBuddy, fork remote --
    combined_ca = setup.auth_proxy.combined_ca if setup.auth_proxy else None
    proxy_url = get_proxy_url(ctx.caller_env)

    # Write kubeconfig when token is available and profile enables it.
    # CLI profile skips this — the user has their own ~/.kube/config.
    k8s_token = setup.k8s_token or settings.k8s_token
    if k8s_token and profile.k8s and profile.write_kubeconfig:
        with tracer.start_as_current_span("write_kubeconfig", context=root_ctx):
            setup.kubeconfig_path = kubeconfig.write_kubeconfig(
                token=k8s_token,
                k8s_cfg=profile.k8s,
                session_dir=session.paths.session_dir,
                combined_ca_path=combined_ca,
                proxy_url=proxy_url,
            )

    # Configure BuildBuddy now that secrets are available.
    with tracer.start_as_current_span("setup_buildbuddy", context=root_ctx):
        if buildbuddy_api_key := setup.buildbuddy_api_key:
            session.buildbuddy_api_key = buildbuddy_api_key
            buildbuddy_result = await run_in_thread(
                lambda: buildbuddy.setup_buildbuddy(api_key=buildbuddy_api_key, session_dir=session.paths.session_dir)
            )
            if isinstance(buildbuddy_result, BaseException):
                logger.warning("Failed to configure BuildBuddy: %s", buildbuddy_result)
            else:
                setup.buildbuddy_setup = buildbuddy_result

    # Write bbr bazelrc (metadata tags for BuildBuddy invocation filtering).
    # Consumed by bbr via $BBR_BAZELRC and try-imported into the session bazelrc.
    session_id = ctx.caller_env.get("CLAUDE_CODE_SESSION_ID", "unknown")
    bbr_bazelrc = session.paths.session_dir / "bbr.bazelrc"
    bbr_bazelrc_content = (
        "# Auto-generated by session start hook — consumed by bbr and session bazelrc\n"
        "build --build_metadata=ROLE=claude-code\n"
        f"build --build_metadata=TAGS=session:{session_id}\n"
    )
    write_config(bbr_bazelrc, bbr_bazelrc_content, "bbr bazelrc")

    # Render session bazelrc
    with tracer.start_as_current_span("render_bazelrc", context=root_ctx):
        bazelrc_template = Template(
            CONFIG_FILES.joinpath("bazelrc.mako").read_text(), imports=["from shlex import quote as sh"]
        )
        bazelrc_content: str = bazelrc_template.render(
            setup_auth_proxy=profile.setup_auth_proxy,
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
            bbr_bazelrc=bbr_bazelrc,
        )
        session_bazelrc = session.paths.session_dir / "bazelrc"
        write_config(session_bazelrc, bazelrc_content, "session bazelrc")

    # Install PATH shims (bazelisk --bazelrc injection + git safety).
    with tracer.start_as_current_span("install_shims", context=root_ctx):
        install_shim("bazelisk", session.paths)
        install_shim("git", session.paths)
        install_shim("bazel", session.paths)
        install_shim("bb", session.paths)
        install_shim("bbr", session.paths)

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
            kubeconfig_path=setup.kubeconfig_path,
            bbr_bazelrc=bbr_bazelrc,
            env_overlay=startup.env_overlay,
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
        env_overlay=setup.env_overlay,
    )

    # Build structured session context for Claude Code transcript
    with tracer.start_as_current_span("emit_session_context", context=root_ctx):
        extra_context = _render_extra_context(
            project_dir,
            setup,
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
            session_id=session_id,
            startup=startup,
        )
        output = HookOutput(hook_specific_output=SessionStartHookSpecificOutput(additional_context=context_output))

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
    setup: PlatformSetup,
    *,
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
        setup=setup,
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
