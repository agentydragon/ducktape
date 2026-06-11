import os
import shutil

import pytest
import pytest_bazel

from sandboxed_jupyter._markers import REQUIRES_SANDBOX_EXEC

# Run these stdio-handshake tests in a dedicated xdist group to avoid flakiness
pytestmark = [*REQUIRES_SANDBOX_EXEC, pytest.mark.shell, pytest.mark.xdist_group("sj_stdio")]

# Mark xfail if external tooling is not available
if not shutil.which("jupyter-mcp-server"):
    pytestmark.append(pytest.mark.xfail(reason="jupyter-mcp-server not installed", strict=False))
if os.environ.get("ADGN_RUN_SJ_STDIO") != "1":
    pytestmark.append(
        pytest.mark.skip(reason="SJ stdio integration requires external tooling; set ADGN_RUN_SJ_STDIO=1 to run")
    )


async def test_wrapper_unsandbox_initialize_and_hello(provision_ws_with_policy, pick_free_port, mcp_client_from_cmd):
    (ws, run_root) = provision_ws_with_policy
    cmd_args = [
        "--workspace",
        str(ws),
        "--run-root",
        str(run_root),
        "--mode",
        "seatbelt",
        "--jupyter-port",
        str(pick_free_port),
        "--no-kernel-sandbox",
    ]

    async with mcp_client_from_cmd("sandbox-jupyter", cmd_args, init_timeout=30.0) as client:
        result = await client.call_tool("append_execute_code_cell", {"cell_source": "print('hello world')"})
        assert "hello world" in str(result)


if __name__ == "__main__":
    pytest_bazel.main()
