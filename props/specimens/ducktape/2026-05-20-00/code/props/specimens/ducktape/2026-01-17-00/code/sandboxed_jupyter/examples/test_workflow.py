import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from mcp_infra.json_helpers import read_line_json_dict, send_line_json

# Constants for readability in comparisons and timing
STARTUP_DRAIN_SECS = 5.0
INIT_ID = 1
TOOLS_LIST_ID = 99
EXEC_OK_ID = 2
EXEC_NET_ID = 4
EXEC_DENIED_ID = 3


@pytest.mark.macos
def test_example_bundle_and_launch(tmp_path):
    # Preconditions: we need jupyter and jupyter-mcp-server resolvable on PATH (hard fail if missing)
    assert shutil.which("jupyter"), "'jupyter' must be on PATH for tests"
    assert shutil.which("jupyter-mcp-server"), "'jupyter-mcp-server' must be on PATH for tests"

    # Ensure our package is importable to subprocesses via PYTHONPATH
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{src_dir}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else str(src_dir)
    env["JUPYTER_LOG_LEVEL"] = "DEBUG"

    bundle_dir = tmp_path / "bundle"
    runtime_dir = tmp_path / "runtime"
    (runtime_dir / "workspace").mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"[test] runtime_dir={runtime_dir.as_posix()}\n")

    venv_root = Path(sys.executable).resolve().parents[1]  # <venv>/bin/.. -> venv root
    composer_cfg = {
        "version": 1,
        "bundle_dir": bundle_dir.as_posix(),
        "runtime_dir": runtime_dir.as_posix(),
        "kernel": {
            "name": "python3",
            "display_name": "Python 3 (sandboxed)",
            "language": "python",
            "argv_base": [sys.executable, "-m", "ipykernel_launcher"],
        },
        "policy": {
            "env": {
                "set": {
                    "JUPYTER_RUNTIME_DIR": f"{runtime_dir.as_posix()}/runtime",
                    "JUPYTER_DATA_DIR": f"{bundle_dir.as_posix()}/data",
                    "JUPYTER_CONFIG_DIR": f"{bundle_dir.as_posix()}/config",
                    "JUPYTER_PATH": f"{bundle_dir.as_posix()}/data",
                    "PYTHONPYCACHEPREFIX": f"{runtime_dir.as_posix()}/pycache",
                    "MPLCONFIGDIR": f"{runtime_dir.as_posix()}/mpl",
                    "HOME": runtime_dir.as_posix(),
                },
                "passthrough": [],
            },
            "fs": {
                # Limit read to python binary dir and stdlib site-packages only
                "read_paths": [venv_root.as_posix(), (venv_root / "lib").as_posix(), bundle_dir.as_posix()],
                "write_paths": [runtime_dir.as_posix(), (runtime_dir / "workspace").as_posix()],
            },
            "net": {"mode": "loopback"},
            "platform": {"seatbelt": {"trace": False}},
        },
    }
    composer_yaml = yaml.safe_dump(composer_cfg, sort_keys=False)

    # Run composer via stdin
    subprocess.run(
        [sys.executable, "-m", "sandboxed_jupyter.jupyter_sandbox_compose", "--config", "-"],
        input=composer_yaml.encode(),
        check=True,
        env=env,
    )

    # Launch the MCP stdio bridge and Jupyter using the generated bundle
    config_dir = bundle_dir / "config"
    kernels_dir = bundle_dir / "kernels"

    assert (config_dir / "jupyter_server_config.py").exists()
    assert (kernels_dir / "python3" / "kernel.json").exists()

    launch_cmd = [
        sys.executable,
        "-m",
        "sandboxed_jupyter.launch",
        "--config",
        config_dir,
        "--kernels",
        kernels_dir,
        "--workspace",
        runtime_dir / "workspace",
        "--kernel-name",
        "python3",
        "--port",
        "0",
        "--token",
        "auto",
        "--start-new-runtime",
        "--log-dir",
        runtime_dir,
    ]

    p = subprocess.Popen(launch_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    # Allow some startup logs to flow, helps MCP readiness
    start = time.time()
    while time.time() - start < STARTUP_DRAIN_SECS:
        line = p.stderr.readline()
        if not line:
            break
        sys.stderr.write("[stderr] " + line.decode(errors="ignore"))

    # MCP stdio protocol: initialize, then execute a code cell
    try:
        send_line_json(
            p.stdin,
            {
                "jsonrpc": "2.0",
                "id": INIT_ID,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "pytest", "version": "0.0.1"},
                },
            },
        )
        init_resp = None
        deadline = time.time() + 45.0
        while time.time() < deadline and not init_resp:
            m = read_line_json_dict(p.stdout, 1.0)
            if m and m.get("id") == INIT_ID and ("result" in m or "error" in m):
                init_resp = m
        assert init_resp is not None, f"initialize failed: {init_resp}"
        assert "result" in init_resp, f"initialize failed: {init_resp}"
        send_line_json(p.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        time.sleep(0.3)
        # List available tools (sanity)
        send_line_json(p.stdin, {"jsonrpc": "2.0", "id": TOOLS_LIST_ID, "method": "tools/list"})
        tools_resp = None
        deadline = time.time() + 45.0
        while time.time() < deadline and not tools_resp:
            m = read_line_json_dict(p.stdout, 1.0)
            if m and m.get("id") == TOOLS_LIST_ID and ("result" in m or "error" in m):
                tools_resp = m
        assert tools_resp is not None, f"tools/list failed: {tools_resp}"
        assert "result" in tools_resp, f"tools/list failed: {tools_resp}"

        # Happy-path execution
        code_ok = "print('OK:', 2+2)"
        send_line_json(
            p.stdin,
            {
                "jsonrpc": "2.0",
                "id": EXEC_OK_ID,
                "method": "tools/call",
                "params": {"name": "append_execute_code_cell", "arguments": {"cell_source": code_ok}},
            },
        )
        exec_ok = None
        deadline = time.time() + 45.0
        while time.time() < deadline and not exec_ok:
            m = read_line_json_dict(p.stdout, 1.0)
            if m and m.get("id") == EXEC_OK_ID and ("result" in m or "error" in m):
                exec_ok = m
        assert exec_ok is not None, f"code exec failed: {exec_ok}"
        assert "result" in exec_ok, f"code exec failed: {exec_ok}"

    finally:
        with contextlib.suppress(Exception):
            p.terminate()
        with contextlib.suppress(Exception):
            p.wait(timeout=5)
