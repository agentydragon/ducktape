from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from urllib.parse import urlunparse

import docker
import yaml

from mcp_infra.constants import SLEEP_FOREVER_CMD
from net_util.net import pick_free_port, wait_for_port
from sandboxed_jupyter._jupyter_shared import (
    JUPYTER_SERVER_CONFIG,
    build_jupyter_mcp_command,
    build_jupyter_server_command,
)
from sandboxed_jupyter.sandboxer import Policy, build_sandboxer_command

StrPath = str | PathLike[str]


def _ensure_document_id(workspace: Path, document_id: str | None) -> str:
    if document_id is not None:
        resolved_id = document_id
        target_path = workspace / document_id
    else:
        rel = (
            Path(".mcp")
            / "scratch"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4) + ".ipynb")
        )
        resolved_id = str(rel)
        target_path = workspace / rel
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        kernelspec = {"name": "python3", "display_name": "Python 3", "language": "python"}
        target_path.write_text(
            json.dumps({"cells": [], "metadata": {"kernelspec": kernelspec}, "nbformat": 4, "nbformat_minor": 5})
        )
    return resolved_id


# Docker mode helper (unchanged behavior aside from workspace/run_root coming from CLI)


def _build_bash_script(document_id: str, token: str, start_new_runtime: bool) -> str:
    """Build bash script for Docker mode that starts Jupyter server and MCP server.

    The workspace path is not needed here since it's always /workspace in the container.
    """
    dq = shlex.quote(document_id)
    tq = shlex.quote(token)
    return f"""
set -euo pipefail
trap 'kill "$JPID" 2>/dev/null || true' EXIT
# Launch Jupyter Server in background with logs redirected to runtime dir
jupyter server \
  --port "$JP_PORT" \
  --ip 127.0.0.1 \
  --ServerApp.root_dir /workspace \
  --ServerApp.open_browser=False \
  --ServerApp.token {tq} \
  --ServerApp.password '' \
  --ServerApp.disable_check_xsrf True \
  1>"$JUPYTER_RUNTIME_DIR/jupyter_server.out" 2>"$JUPYTER_RUNTIME_DIR/jupyter_server.err" &
JPID=$!
# Wait for port to become ready (up to ~10s)
# Note: this inline probe intentionally mirrors adgn.util.net.wait_for_port.
# The wrapper runs in an isolated environment where importing adgn is not guaranteed.
python3 - "$JP_PORT" <<'PY'
import socket, sys, time
port=int(sys.argv[1])
for _ in range(20):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)
sys.exit(1)
PY
# Now exec the stdio MCP server in foreground (inherits stdio)
exec jupyter-mcp-server start \
  --transport stdio \
  --provider jupyter \
  --document-url http://127.0.0.1:$JP_PORT \
  --document-id {dq} \
  --document-token {tq} \
  --runtime-url http://127.0.0.1:$JP_PORT \
  --runtime-token {tq} \
  --start-new-runtime {("true" if start_new_runtime else "false")}
""".strip()


def _docker(workspace: Path, document_id: str, docker_image: str, start_new_runtime: bool, jupyter_port: int) -> int:
    token = secrets.token_urlsafe(24)
    bash_script = _build_bash_script(document_id, token, start_new_runtime)

    run_root = f"/tmp/sjmcp-{secrets.token_hex(6)}"

    # Prepare environment for the container
    env = {
        "JP_PORT": str(jupyter_port),
        "JUPYTER_RUNTIME_DIR": f"{run_root}/runtime",
        "JUPYTER_DATA_DIR": f"{run_root}/data",
        "JUPYTER_CONFIG_DIR": f"{run_root}/config",
        "MPLCONFIGDIR": f"{run_root}/mpl",
    }

    # Start a background container via Docker SDK; keep it alive and exec the script to preserve stdio semantics
    name = f"sjmcp-{secrets.token_hex(6)}"
    try:
        dclient = docker.from_env()
        dclient.ping()
    except Exception as e:
        print(f"[wrapper] ERROR: Docker daemon not reachable: {e}", file=sys.stderr)
        return 2

    container = None
    try:
        try:
            # Standardize long-lived container command across wrappers

            container = dclient.containers.run(
                image=docker_image,
                command=SLEEP_FOREVER_CMD,
                name=name,
                remove=True,
                detach=True,
                environment=env,
                volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                working_dir="/workspace",
            )
        except Exception as e:
            print(f"[wrapper] ERROR: failed to start container: {e}", file=sys.stderr)
            return 2

        # Run the jupyter+mcp startup script inside the container attached to our stdio
        exec_cmd = ["docker", "exec", "-i", name, "bash", "-lc", bash_script]
        return subprocess.Popen(exec_cmd).wait()
    finally:
        try:
            if container is not None:
                container.stop()
        except Exception as e:
            print(f"[wrapper] WARN: failed to stop container {name}: {e}", file=sys.stderr)


# Jupyter helpers


def _kernels_dir(run_root: Path) -> Path:
    return run_root / "data" / "kernels"


def _write_sandboxed_kernelspec(
    run_root: Path, workspace: Path, policy_yaml: Path, kernel_python: str, *, trace: bool
) -> None:
    # Override the default 'python3' kernel to ensure the sandbox is used.
    ks_dir = _kernels_dir(run_root) / "python3"
    ks_dir.mkdir(parents=True, exist_ok=True)
    # Always exec kernel via our tiny exec wrapper to capture stderr reliably;
    # it will choose shim vs ipykernel based on SJ_DEBUG_DIAG
    kernel_cmd = [
        kernel_python,
        "-m",
        "sandboxed_jupyter.kernel_exec",
        "--stderr-log",
        str((run_root / "runtime" / "kernel_stderr.log").as_posix()),
        "--",
        "-f",
        "{connection_file}",
    ]
    # Enable sandboxer debug when SJ_DEBUG_DIAG is set to surface policy path and -D params
    argv = build_sandboxer_command(policy_yaml, kernel_cmd, trace=trace, debug=bool(os.environ.get("SJ_DEBUG_DIAG")))
    kernel_env = {"SJ_KERNEL_SANDBOXED": "1", "SJ_POLICY_PATH": str(policy_yaml)}
    # If diagnostics are enabled, increase kernel-side verbosity to aid debugging
    if os.environ.get("SJ_DEBUG_DIAG"):
        kernel_env.update({"PYTHONFAULTHANDLER": "1", "PYTHONVERBOSE": "1"})
    kernel_json = {"argv": argv, "display_name": "Python 3", "language": "python", "env": kernel_env}
    (ks_dir / "kernel.json").write_text(json.dumps(kernel_json))


def _start_jupyter_server(
    workspace: Path,
    token: str,
    jupyter_port: int,
    run_root: Path,
    env: dict[str, str],
    kernel_default_name: str | None = None,
) -> tuple[subprocess.Popen, int]:
    """Start Jupyter server with wrapper-specific configuration.

    This differs from the launch.py version by:
    - Auto-selecting a free port if jupyter_port is 0
    - Using DEBUG log level
    - Setting JUPYTER_PLATFORM_DIRS env var
    - Returning both process and actual port
    """
    out_path = run_root / "runtime" / "jupyter_server.out"
    err_path = run_root / "runtime" / "jupyter_server.err"
    # Honor port=0 by selecting a free local port ourselves
    port = jupyter_port if int(jupyter_port) != 0 else pick_free_port()

    cmd = build_jupyter_server_command(
        port=port,
        workspace=workspace,
        token=token,
        config_file=run_root / "config" / "jupyter_server_config.py",
        log_level="DEBUG",
        kernel_default_name=kernel_default_name,
    )

    # Turn on RTC/ydoc deps visibility and quieter platformdirs warning
    env = dict(env)
    env.setdefault("JUPYTER_PLATFORM_DIRS", "1")

    # Open log files, start subprocess, then close our handles (subprocess keeps its copies)
    with out_path.open("a", buffering=1) as out_f, err_path.open("a", buffering=1) as err_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, env=env)

    wait_for_port("127.0.0.1", port, timeout_secs=10.0)
    return proc, port


# Seatbelt mode


def _seatbelt(
    *,
    workspace: Path,
    run_root: Path,
    document_id: str,
    start_new_runtime: bool,
    jupyter_port: int,
    env_set: dict[str, str],
    kernel_default_name: str | None,
    # Policy from YAML
    policy: Policy,
    policy_yaml_path: Path,
    # Kernel interpreter selection
    kernel_python: str,
    # Diagnostics
    trace: bool,
) -> int:
    # Ensure fresh runtime dir for logs/config; use provided run_root
    for subdir in ("runtime", "config", "data", "mpl", "scratch"):
        (run_root / subdir).mkdir(parents=True, exist_ok=True)

    # Write standard Jupyter Server config
    (run_root / "config" / "jupyter_server_config.py").write_text(JUPYTER_SERVER_CONFIG)

    # Document ID is already ensured by run_seatbelt() before calling _seatbelt()
    # No need to call _ensure_document_id() here

    # Write the sandboxed kernelspec and start server
    _write_sandboxed_kernelspec(run_root, workspace, policy_yaml_path, kernel_python, trace=trace)

    # Inherit env, but override for Jupyter-specific locations
    env = dict(os.environ)
    env.update(env_set)
    # Start Jupyter and keep the auth token for MCP connections
    jpy_token = secrets.token_urlsafe(24)
    proc, actual_port = _start_jupyter_server(workspace, jpy_token, jupyter_port, run_root, env, kernel_default_name)

    # Launch stdio MCP server bound to our stdio to drive the Jupyter server
    mcp_url = urlunparse(("http", f"127.0.0.1:{actual_port}", "", "", "", ""))
    mcp_cmd = build_jupyter_mcp_command(
        base_url=mcp_url, document_id=document_id, token=jpy_token, start_new_runtime=start_new_runtime
    )

    try:
        # Inherit stdio so pytest can speak JSON-RPC to the MCP server
        mcp_rc = subprocess.Popen(mcp_cmd).wait()
        return int(mcp_rc)
    finally:
        with contextlib.suppress(Exception):
            proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _ensure_workspace(value: str | None) -> Path:
    if not value:
        return Path.cwd()
    p = Path(value)
    p.mkdir(parents=True, exist_ok=True)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandboxed_jupyter",
        description="Run Jupyter under a sandboxed kernel and expose via jupyter-mcp-server (stdio)",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # Seatbelt mode: local process, sandboxed kernel
    p_loc = sub.add_parser("seatbelt", help="Run locally with SBPL sandboxed kernel (stdio)")
    # Support both positional and legacy --workspace flag (tests may use either)
    p_loc.add_argument("workspace", nargs="?", type=Path, help="Workspace directory for notebooks and runtime state")
    p_loc.add_argument("--workspace", dest="workspace_opt", type=Path, help="Workspace directory (legacy flag)")
    p_loc.add_argument(
        "--document-id", help="Notebook path relative to workspace; if missing, a new timestamped notebook is created"
    )
    p_loc.add_argument("--policy", required=True, type=Path, help="Path to SBPL policy YAML")
    p_loc.add_argument("--kernel-python", default=sys.executable, help="Python interpreter to run the kernel with")
    p_loc.add_argument(
        "--kernel-default-name", default=None, help="Default kernel name for new notebooks (e.g., python3)"
    )
    p_loc.add_argument("--jupyter-port", type=int, default=8899, help="Local port for Jupyter server")
    p_loc.add_argument("--trace", action="store_true", help="Enable sandboxer debug and kernel diag shim")

    # Docker mode: background container with Jupyter + stdio MCP
    p_dock = sub.add_parser("docker", help="Run inside Docker and exec jupyter-mcp-server (stdio)")
    p_dock.add_argument("workspace", type=Path, help="Workspace directory mounted into container at /workspace")
    p_dock.add_argument(
        "--document-id", help="Notebook path relative to workspace; if missing, a new timestamped notebook is created"
    )
    p_dock.add_argument(
        "--docker-image", required=True, help="Docker image with jupyter + jupyter-mcp-server available"
    )
    p_dock.add_argument(
        "--start-new-runtime",
        action="store_true",
        help="Start a fresh runtime for the document (cold start) vs reusing one if present",
    )
    p_dock.add_argument("--jupyter-port", type=int, default=8899, help="TCP port to expose inside the container")

    # Common options
    parser.add_argument(
        "--run-root", default=str(Path.cwd() / ".mcp"), help="Root directory for runtime logs/config/state"
    )
    parser.add_argument("--trace", action="store_true", help="Enable sandboxer debug output (SJ_DEBUG_DIAG)")

    args = parser.parse_args(argv)

    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    for subdir in ("runtime", "config", "data", "mpl"):
        (run_root / subdir).mkdir(parents=True, exist_ok=True)

    # Diagnostics
    if args.trace:
        os.environ.setdefault("SJ_DEBUG_DIAG", "1")

    if args.mode == "seatbelt":
        # Resolve workspace from positional or legacy flag
        ws_arg = args.workspace or args.workspace_opt
        if not ws_arg:
            parser.error("workspace is required (positional or --workspace)")
        return run_seatbelt(
            workspace=ws_arg,
            run_root=run_root,
            policy_yaml=args.policy,
            kernel_python=args.kernel_python,
            document_id=args.document_id,
            kernel_default_name=args.kernel_default_name,
            jupyter_port=args.jupyter_port,
            trace=args.trace,
            env_set={},
            start_new_runtime=True,
        )

    if args.mode == "docker":
        return run_docker(
            workspace=args.workspace,
            docker_image=args.docker_image,
            document_id=args.document_id,
            start_new_runtime=args.start_new_runtime,
            jupyter_port=args.jupyter_port,
        )

    parser.error("unknown mode")
    return 2


def run_seatbelt(
    *,
    workspace: StrPath,
    run_root: StrPath,
    policy_yaml: StrPath,
    kernel_python: StrPath,
    document_id: StrPath | None = None,
    kernel_default_name: str | None = None,
    jupyter_port: int = 8899,
    trace: bool = False,
    env_set: dict[str, str] | None = None,
    start_new_runtime: bool = True,
) -> int:
    ws = _ensure_workspace(str(workspace))
    rr = Path(run_root).expanduser().resolve()
    pol = Path(policy_yaml).expanduser().resolve()
    # Ensure document id exists (create if missing)
    doc_id = _ensure_document_id(ws, None if document_id is None else str(document_id))
    try:
        raw = yaml.safe_load(pol.read_text())
    except Exception as e:
        print(f"[wrapper] invalid policy YAML: {e}", file=sys.stderr)
        return 2
    try:
        policy = Policy(**(raw or {}))
    except Exception as e:
        print(f"[wrapper] policy schema error: {e}", file=sys.stderr)
        return 2
    return _seatbelt(
        workspace=ws,
        run_root=rr,
        document_id=doc_id,
        start_new_runtime=start_new_runtime,
        jupyter_port=int(jupyter_port),
        env_set=env_set or {},
        kernel_default_name=kernel_default_name,
        policy=policy,
        policy_yaml_path=pol,
        kernel_python=str(kernel_python),
        trace=bool(trace),
    )


def run_docker(
    *,
    workspace: StrPath,
    docker_image: str,
    document_id: StrPath | None = None,
    start_new_runtime: bool = False,
    jupyter_port: int = 8899,
) -> int:
    ws = _ensure_workspace(str(workspace))
    doc_id = _ensure_document_id(ws, None if document_id is None else str(document_id))
    return _docker(ws, doc_id, docker_image, start_new_runtime, int(jupyter_port))


if __name__ == "__main__":
    raise SystemExit(main())
