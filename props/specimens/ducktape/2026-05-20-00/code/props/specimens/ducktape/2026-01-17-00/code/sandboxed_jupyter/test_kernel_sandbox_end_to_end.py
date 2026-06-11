from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ._markers import REQUIRES_SANDBOX_EXEC

# Run these stdio-handshake tests in a dedicated xdist group to avoid flakiness
pytestmark = [*REQUIRES_SANDBOX_EXEC, pytest.mark.shell, pytest.mark.xdist_group("sj_stdio")]

# Mark xfail if external tooling is not available
if not shutil.which("jupyter-mcp-server"):
    pytestmark.append(pytest.mark.xfail(reason="jupyter-mcp-server not installed", strict=False))
# Allow opt-in to actually run these heavy integration tests.
if os.environ.get("ADGN_RUN_SJ_STDIO") != "1":
    pytestmark.append(
        pytest.mark.skip(reason="SJ stdio integration requires external tooling; set ADGN_RUN_SJ_STDIO=1 to run")
    )

# Ensure required jupyter kernel/server packages are present — with pyproject deps these should be installed


async def test_kernel_runs_minimal(tmp_path: Path, mcp_client_from_cmd):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "workspace").mkdir(parents=True, exist_ok=True)

    cmd_args = [
        "seatbelt",
        str(ws),
        "--run-root",
        str(tmp_path / ".mcp"),
        "--policy",
        str(tmp_path / "policy.yaml"),
        "--kernel-python",
        os.environ.get("PYTHON", "python3"),
        "--jupyter-port",
        "0",
    ]
    # Write a minimal policy.yaml
    (tmp_path / "policy.yaml").write_text(
        """
env:
  set:
    HOME: {home}
fs:
  read_paths: ['/']
  write_paths: ['{home}']
net: {{ mode: loopback }}
""".format(home=str(tmp_path)),
        encoding="utf-8",
    )

    async with mcp_client_from_cmd("sandbox-jupyter", cmd_args, init_timeout=45.0) as client:
        result = await client.call_tool("append_execute_code_cell", {"cell_source": "print('OK')"})
        assert result is not None
        assert "OK" in str(result)
