"""Hook daemon entry point — starts uvicorn on a Unix domain socket."""

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from filelock import FileLock

from devinfra.claude.hook_daemon.config import OtelConfig, ProfileConfig
from devinfra.claude.hook_daemon.server import create_app
from devinfra.claude.hook_daemon.tracing import init_daemon_tracing, shutdown_tracing
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)


def _resolve_otel_config(profile: ProfileConfig) -> OtelConfig | None:
    """Build OtelConfig from profile + env vars.

    Bearer token: web — injected via settings.local.json by web_setup.sh;
    CLI — sourced from .envrc (cli_env.sh) before daemon starts.
    """
    if not profile.otel:
        return None

    otel_config = profile.otel.with_env_overrides()
    if not otel_config.endpoint:
        return None

    token = os.environ.get("DUCKTAPE_OTEL_BEARER_TOKEN")
    if token:
        otel_config = OtelConfig(endpoint=otel_config.endpoint, bearer_token=token)

    return otel_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Hook daemon")
    parser.add_argument("--sock", type=str, required=True, help="UDS path to listen on")
    parser.add_argument("--daemon-dir", type=str, required=True, help="Directory for logs, env persistence")
    args = parser.parse_args()

    daemon_dir = Path(args.daemon_dir)
    daemon_dir.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive flock on pidfile — held for daemon lifetime.
    # The kernel releases it on process death (flock is fd-based), so clients
    # can probe the lock to determine liveness without PID-reuse ambiguity.
    pidfile = daemon_dir / "daemon.pid"
    _pidfile_lock = FileLock(str(pidfile))
    _pidfile_lock.acquire()
    pidfile.write_text(str(os.getpid()))

    log_file = daemon_dir / "daemon.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
    )

    # Log all env var keys available at daemon startup (before any session start hook runs).
    # Values are omitted to avoid leaking secrets into logs.
    logger.info("Daemon startup env var keys: %s", sorted(os.environ))
    logger.info("Daemon startup settings: %s", HookSettings().model_dump())

    # Load profile once at daemon startup.
    project_dir_str = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir_str:
        raise RuntimeError("CLAUDE_PROJECT_DIR not set — cannot load profile config")

    project_dir = Path(project_dir_str)
    settings = HookSettings()
    if not settings.profile:
        raise RuntimeError("DUCKTAPE_CLAUDE_HOOKS_PROFILE not set — cannot load profile config")
    profile = ProfileConfig.load(project_dir / settings.profile)

    otel_config = _resolve_otel_config(profile)

    init_daemon_tracing(daemon_dir, otel_config=otel_config)
    app = create_app(daemon_dir, profile=profile)
    uvicorn.run(app, uds=args.sock, log_level="info")
    shutdown_tracing()


if __name__ == "__main__":
    main()
