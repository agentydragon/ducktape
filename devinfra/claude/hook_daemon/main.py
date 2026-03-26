"""Hook daemon entry point — starts uvicorn on a Unix domain socket."""

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from filelock import FileLock

from devinfra.claude.hook_daemon.server import app, configure
from devinfra.claude.hook_daemon.tracing import init_daemon_tracing
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)


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
    _pidfile_lock = FileLock(pidfile)
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

    otlp_exporter = init_daemon_tracing(daemon_dir)
    configure(daemon_dir, otlp_exporter)

    uvicorn.run(app, uds=args.sock, log_level="warning")


if __name__ == "__main__":
    main()
