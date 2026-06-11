from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from jupyter_client import BlockingKernelClient

logger = logging.getLogger(__name__)

SESSION_DIR = Path(os.environ.get("EMBER_PYTHON_SESSION_DIR", "/var/run/ember/python"))
CONNECTION_FILE = SESSION_DIR / "kernel.json"
PID_FILE = SESSION_DIR / "kernel.pid"


def ensure_kernel() -> Path | None:
    """Ensure a persistent IPython kernel is running and return its connection file."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create python session directory %s: %s", SESSION_DIR, exc)
        return None

    if CONNECTION_FILE.exists() and _kernel_alive():
        return CONNECTION_FILE

    _cleanup_stale_files()
    return _launch_kernel()


def connection_file() -> Path | None:
    if CONNECTION_FILE.exists() and _kernel_alive():
        return CONNECTION_FILE
    return None


def stop_kernel(timeout: float = 5.0) -> bool:
    pid = _kernel_pid()
    if pid is None:
        _cleanup_stale_files()
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_stale_files()
        return False
    except OSError as exc:  # pragma: no cover
        logger.warning("Failed to signal kernel process %s: %s", pid, exc)
        return False

    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if not _kernel_alive():
            _cleanup_stale_files()
            logger.info("Stopped IPython kernel pid=%s", pid)
            return True
        time.sleep(0.1)

    logger.warning("Timed out stopping kernel pid=%s", pid)
    return False


def restart_kernel() -> Path | None:
    stop_kernel()
    return ensure_kernel()


def run_code(code: str) -> str:
    conn = ensure_kernel()
    if conn is None:
        raise RuntimeError("Persistent kernel unavailable (ipykernel not installed?)")

    client = BlockingKernelClient()
    client.load_connection_file(str(conn))
    client.start_channels()
    try:
        msg_id = client.execute(code)
        client.get_shell_msg(timeout=10)
        output_parts: list[str] = []
        while True:
            msg = client.get_iopub_msg(timeout=10)
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            msg_type = msg.get("msg_type")
            if msg_type == "status" and msg.get("content", {}).get("execution_state") == "idle":
                break
            if msg_type == "stream":
                output_parts.append(msg["content"].get("text", ""))
            elif msg_type == "error":
                output_parts.append("\n".join(msg.get("content", {}).get("traceback", [])))
        return "".join(output_parts)
    finally:
        client.stop_channels()


def cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interact with Ember's persistent Python session")
    parser.add_argument("-c", dest="command", help="Python code to execute (default: read stdin)")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress output from the executed code")
    parser.add_argument("--stop", action="store_true", help="Stop the kernel and exit")
    parser.add_argument("--restart", action="store_true", help="Restart the kernel before running code")
    parser.add_argument("--status", action="store_true", help="Print kernel status and exit")
    args = parser.parse_args(argv)

    if args.status:
        _print_status()
        return 0

    if args.stop:
        stopped = stop_kernel()
        _print_status()
        return 0 if stopped else 1

    if args.restart:
        restart_kernel()

    code = args.command if args.command is not None else sys.stdin.read()

    if not code.strip():
        return 0

    try:
        output = run_code(code)
    except Exception as exc:  # pragma: no cover - CLI convenience
        print(f"ember-python: {exc}", file=sys.stderr)
        return 1

    if not args.quiet and output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _kernel_alive() -> bool:
    pid = _kernel_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kernel_pid() -> int | None:
    try:
        return int(PID_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _print_status() -> None:
    if _kernel_alive():
        conn = CONNECTION_FILE if CONNECTION_FILE.exists() else "(missing connection file)"
        pid = _kernel_pid()
        print(f"Kernel running (pid={pid}) connection={conn}")
    else:
        print("Kernel not running")


def _cleanup_stale_files() -> None:
    for path in (CONNECTION_FILE, PID_FILE):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.debug("Failed to remove stale file %s: %s", path, exc)


def _launch_kernel() -> Path | None:
    cmd = [sys.executable, "-m", "ipykernel_launcher", "-f", str(CONNECTION_FILE)]
    try:
        env = os.environ.copy()
        workspace = env.get("EMBER_WORKSPACE_DIR")
        if workspace:
            env.setdefault("IPYTHONDIR", workspace)
            pythonpath = env.get("PYTHONPATH", "")
            parts = [workspace]
            if pythonpath:
                parts.append(pythonpath)
            env["PYTHONPATH"] = ":".join(parts)

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        logger.warning("ipykernel is not installed; persistent Python session unavailable.")
        return None
    except Exception as exc:  # pragma: no cover - subprocess env errors
        logger.warning("Failed to start IPython kernel: %s", exc)
        return None

    PID_FILE.write_text(str(proc.pid))

    for _ in range(50):
        if CONNECTION_FILE.exists():
            logger.info("Started persistent IPython kernel pid=%s connection=%s", proc.pid, CONNECTION_FILE)
            _initialize_kernel_environment(workspace)
            return CONNECTION_FILE
        time.sleep(0.1)

    logger.warning("IPython kernel did not create connection file %s", CONNECTION_FILE)
    return None


def _initialize_kernel_environment(workspace: str | None) -> None:
    """Ensure the persistent kernel has the workspace on sys.path."""
    client = BlockingKernelClient()
    client.load_connection_file(str(CONNECTION_FILE))
    client.start_channels()
    try:
        code = textwrap.dedent(
            f"""
            import os, sys
            _wp = {workspace!r}
            if _wp and _wp not in sys.path:
                sys.path.insert(0, _wp)
            """
        )
        msg_id = client.execute(code)
        client.get_shell_msg(timeout=10)
        while True:
            msg = client.get_iopub_msg(timeout=10)
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle":
                break
    finally:
        client.stop_channels()
