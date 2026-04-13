"""Hook daemon entry point — starts uvicorn on a Unix domain socket."""

import argparse
import logging
import os
import shlex
import subprocess
from pathlib import Path

import uvicorn
from filelock import FileLock

from devinfra.claude.hook_daemon.config import OtelConfig, ProfileConfig
from devinfra.claude.hook_daemon.models import StartupResult
from devinfra.claude.hook_daemon.server import create_app
from devinfra.claude.hook_daemon.tracing import init_daemon_tracing, shutdown_tracing
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)


def _source_env_script(script_path: Path) -> StartupResult:
    """Run a startup env script and collect env var patches without mutating os.environ.

    The script must print `export VAR=value` lines to stdout (as try_export does).
    Runs as: eval "$(script)" && env -0 — executes the script in a subprocess,
    evals its stdout, then dumps the final env via null-delimited pairs. Returns
    the vars the script added or changed relative to the current env; callers
    apply them explicitly rather than relying on a global mutation.
    """
    if not script_path.exists():
        msg = f"startup_env_script not found: {script_path}"
        logger.warning(msg)
        return StartupResult(exit_code=1, output=msg)

    logger.info("Running startup_env_script: %s", script_path)
    initial_env = dict(os.environ)
    proc = subprocess.run(
        ["bash", "-c", f'eval "$({shlex.quote(str(script_path))})" && env -0'],
        capture_output=True,
        env=initial_env,
        check=False,
    )

    output = proc.stderr.decode(errors="replace")
    if output.strip():
        logger.info("startup_env_script output:\n%s", output.rstrip())

    if proc.returncode != 0:
        logger.warning("startup_env_script exited %d — secrets may be missing from session env", proc.returncode)
        return StartupResult(exit_code=proc.returncode, output=output)

    # Parse null-delimited KEY=VALUE pairs from `env -0`. Collect vars the script
    # added or changed relative to initial_env; do NOT mutate os.environ.
    added: dict[str, str] = {}
    for item in proc.stdout.split(b"\x00"):
        if not item:
            continue
        key_b, _, val_b = item.partition(b"=")
        key = key_b.decode(errors="replace")
        val = val_b.decode(errors="replace")
        if initial_env.get(key) != val:
            added[key] = val

    logger.info("startup_env_script: collected %d new/updated vars: %s", len(added), sorted(added))
    return StartupResult(exit_code=0, output=output, env_overlay=added)


def _resolve_otel_config(profile: ProfileConfig, env_overlay: dict[str, str]) -> OtelConfig | None:
    """Build OtelConfig from profile + env vars.

    Bearer token: web — decrypted via startup_env_script (web_env.sh) at daemon startup;
    CLI — sourced from .envrc (cli_env.sh) before daemon starts.
    """
    if not profile.otel:
        return None

    otel_config = profile.otel.with_env_overrides()
    if not otel_config.endpoint:
        return None

    token = env_overlay.get("DUCKTAPE_OTEL_BEARER_TOKEN") or os.environ.get("DUCKTAPE_OTEL_BEARER_TOKEN")
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

    startup = StartupResult()
    if profile.startup_env_script:
        startup = _source_env_script(project_dir / profile.startup_env_script)

    otel_config = _resolve_otel_config(profile, startup.env_overlay)

    init_daemon_tracing(daemon_dir, otel_config=otel_config)
    app = create_app(daemon_dir, profile=profile, startup=startup)
    uvicorn.run(app, uds=args.sock, log_level="info")
    shutdown_tracing()


if __name__ == "__main__":
    main()
