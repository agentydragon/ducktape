#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlunparse

from net_util.net import pick_free_port
from sandboxed_jupyter._jupyter_shared import build_jupyter_mcp_command, start_jupyter_server_process

StrPath = str | os.PathLike[str]


def run_jupyter_mcp(
    *,
    config_dir: StrPath,
    kernels_dir: StrPath,
    workspace: StrPath,
    kernel_name: str = "python3",
    port: int = 0,
    token: str = "auto",
    start_new_runtime: bool = False,
    log_dir: StrPath | None = None,
) -> int:
    """Programmatic entrypoint that accepts str or Path-like arguments."""
    config_dir = Path(config_dir).resolve()
    kernels_dir = Path(kernels_dir).resolve()
    workspace = Path(workspace).resolve()
    log_dir_path = Path(log_dir).resolve() if log_dir else None

    if not (config_dir / "jupyter_server_config.py").exists():
        print(f"jupyter-mcp-launch: config file not found: {config_dir / 'jupyter_server_config.py'}", file=sys.stderr)
        return 2
    if not kernels_dir.exists():
        print(f"jupyter-mcp-launch: kernels dir not found: {kernels_dir}", file=sys.stderr)
        return 2
    if not workspace.exists():
        print(f"jupyter-mcp-launch: workspace not found: {workspace}", file=sys.stderr)
        return 2

    # Ensure Jupyter sees only our kernels
    child_env = {
        "JUPYTER_PATH": str(kernels_dir.parent),
        "JUPYTER_DATA_DIR": str(kernels_dir.parent),
        "JUPYTER_CONFIG_DIR": str(config_dir),
    }

    # Write a minimal kernels.json to hint default
    runtime_dir = log_dir_path or (config_dir.parent / "runtime")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "kernels.json").write_text(json.dumps({"default": kernel_name}) + "\n")

    eff_port = port or pick_free_port()
    eff_token = secrets.token_urlsafe(24) if token == "auto" else token

    # Ensure we have jupyter and jupyter-mcp-server on PATH
    if not (shutil.which("jupyter") and shutil.which("jupyter-mcp-server")):
        print("jupyter-mcp-launch: 'jupyter' and/or 'jupyter-mcp-server' not found on PATH", file=sys.stderr)
        return 3

    jpy_url = urlunparse(("http", f"127.0.0.1:{eff_port}", "", "", "", ""))
    print(f"[launch] jupyter @ {jpy_url} token=REDACTED", file=sys.stderr)

    jl = start_jupyter_server_process(
        workspace=workspace,
        config_dir=config_dir,
        port=eff_port,
        token=eff_token,
        log_dir=log_dir_path,
        extra_env=child_env,
    )

    mcp_cmd = build_jupyter_mcp_command(
        base_url=jpy_url, document_id=".mcp/auto.ipynb", token=eff_token, start_new_runtime=start_new_runtime
    )

    try:
        proc = subprocess.Popen(mcp_cmd)
        return proc.wait()
    finally:
        with contextlib.suppress(Exception):
            jl.terminate()
        with contextlib.suppress(Exception):
            jl.wait(timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="jupyter-mcp-launch",
        description=(
            "Launch Jupyter Server (unsandboxed) and jupyter-mcp-server (stdio) using precomposed config and kernels"
        ),
    )
    ap.add_argument(
        "--config", required=True, type=Path, help="Path to Jupyter config dir (contains jupyter_server_config.py)"
    )
    ap.add_argument("--kernels", required=True, type=Path, help="Path to kernels dir (kernelspecs)")
    ap.add_argument("--workspace", required=True, type=Path, help="Absolute path to workspace (ServerApp.root_dir)")
    ap.add_argument("--kernel-name", default="python3", help="Default kernel name for new notebooks (hint)")
    ap.add_argument("--port", type=int, default=0, help="0 = auto-pick free port")
    ap.add_argument("--token", default="auto", help="'auto' to generate a token; or provide explicit token string")
    ap.add_argument("--start-new-runtime", action="store_true", help="Pass through to jupyter-mcp-server")
    ap.add_argument("--log-dir", default=None, type=Path, help="Optional directory for Jupyter/MCP logs")
    args = ap.parse_args()
    return run_jupyter_mcp(
        config_dir=args.config,
        kernels_dir=args.kernels,
        workspace=args.workspace,
        kernel_name=args.kernel_name,
        port=args.port,
        token=args.token,
        start_new_runtime=bool(args.start_new_runtime),
        log_dir=args.log_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
