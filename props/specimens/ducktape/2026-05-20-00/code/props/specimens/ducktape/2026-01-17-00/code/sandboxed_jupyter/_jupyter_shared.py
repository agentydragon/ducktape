"""Shared Jupyter configuration and command building utilities.

This module provides common configuration and command-building logic used by both
launch.py and wrapper.py to avoid duplication while keeping the helpers private
to the sandboxed_jupyter package.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import IO

from net_util.net import wait_for_port

# Jupyter Server configuration template for CORS and basic security settings
JUPYTER_SERVER_CONFIG = (
    "c.NotebookApp.allow_origin = '*'\n"
    "c.NotebookApp.trust_xheaders = True\n"
    "c.ServerApp.tornado_settings = {\n"
    "    'headers': {\n"
    "        'Access-Control-Allow-Origin': '*',\n"
    "        'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',\n"
    "        'Access-Control-Allow-Headers': 'Content-Type, Authorization'\n"
    "    }\n"
    "}\n"
)


def build_jupyter_server_command(
    *,
    port: int,
    workspace: Path,
    token: str,
    config_file: Path,
    log_level: str = "INFO",
    kernel_default_name: str | None = None,
) -> list[str]:
    """Build the jupyter server command with standard arguments.

    Args:
        port: Port for Jupyter server to bind to
        workspace: Root directory for Jupyter notebooks
        token: Authentication token
        config_file: Path to jupyter_server_config.py
        log_level: Log level (INFO, DEBUG, etc.)
        kernel_default_name: Optional default kernel name for new notebooks

    Returns:
        Command list ready for subprocess.Popen
    """
    cmd = [
        "jupyter",
        "server",
        "--port",
        str(port),
        "--ip",
        "127.0.0.1",
        "--ServerApp.root_dir",
        str(workspace),
        "--ServerApp.open_browser",
        "False",
        "--ServerApp.token",
        token,
        "--ServerApp.password",
        "",
        "--ServerApp.disable_check_xsrf",
        "True",
        "--ServerApp.log_level",
        log_level,
        "--config",
        str(config_file),
    ]
    if kernel_default_name:
        cmd += [
            "--ServerApp.default_kernel_name",
            kernel_default_name,
            "--NotebookApp.default_kernel_name",
            kernel_default_name,
        ]
    return cmd


def build_jupyter_mcp_command(*, base_url: str, document_id: str, token: str, start_new_runtime: bool) -> list[str]:
    """Build the jupyter-mcp-server command with standard arguments.

    Args:
        base_url: Base URL for Jupyter server (e.g., "http://127.0.0.1:8899")
        document_id: Notebook path relative to workspace
        token: Authentication token
        start_new_runtime: Whether to start a fresh runtime

    Returns:
        Command list ready for subprocess.Popen
    """
    return [
        "jupyter-mcp-server",
        "start",
        "--transport",
        "stdio",
        "--provider",
        "jupyter",
        "--document-url",
        base_url,
        "--document-id",
        document_id,
        "--document-token",
        token,
        "--runtime-url",
        base_url,
        "--runtime-token",
        token,
        "--start-new-runtime",
        "true" if start_new_runtime else "false",
    ]


def start_jupyter_server_process(
    *,
    workspace: Path,
    config_dir: Path,
    port: int,
    token: str,
    log_dir: Path | None,
    extra_env: dict[str, str] | None = None,
    log_level: str = "INFO",
    kernel_default_name: str | None = None,
) -> subprocess.Popen:
    """Start a Jupyter server process with standard configuration.

    Args:
        workspace: Root directory for Jupyter notebooks
        config_dir: Directory containing jupyter_server_config.py
        port: Port for Jupyter server to bind to
        token: Authentication token
        log_dir: Optional directory for logs (or None for DEVNULL)
        extra_env: Optional environment variables to add/override
        log_level: Jupyter log level (INFO, DEBUG, etc.)
        kernel_default_name: Optional default kernel name for new notebooks

    Returns:
        Running Jupyter server process
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # Honor explicit config dir (contains jupyter_server_config.py)
    # Jupyter honors JUPYTER_CONFIG_DIR; also pass --config to be explicit
    env.setdefault("JUPYTER_CONFIG_DIR", str(config_dir))

    cmd = build_jupyter_server_command(
        port=port,
        workspace=workspace,
        token=token,
        config_file=config_dir / "jupyter_server_config.py",
        log_level=log_level,
        kernel_default_name=kernel_default_name,
    )

    out_ctx: AbstractContextManager[IO | int]
    err_ctx: AbstractContextManager[IO | int]
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        out_ctx = (log_dir / "jupyter_server.out").open("a", buffering=1)
        err_ctx = (log_dir / "jupyter_server.err").open("a", buffering=1)
    else:
        out_ctx = nullcontext(subprocess.DEVNULL)
        err_ctx = nullcontext(subprocess.DEVNULL)

    # Open log files (if any), start subprocess, then close our handles (subprocess keeps its copies)
    with out_ctx as out_f, err_ctx as err_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, env=env)

    # Wait for readiness (up to 12s)
    wait_for_port("127.0.0.1", port, timeout_secs=12.0)
    return proc
