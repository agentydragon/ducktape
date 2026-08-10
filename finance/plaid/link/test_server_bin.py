"""The container entrypoint is a different execution shape from every other test here.

`server_bin` runs `app.py` as `__main__` (`aspect_py_binary(main = "app.py")`), so its
`__package__` is empty — while `app_cli` (`main_module`) and every test import it as a module.
Module-level code that resolves anything relative to the package therefore works everywhere
except in the image, which is how a `resources.files(__package__)` crash-looped production while
the whole suite stayed green.

This runs the actual binary and asserts it gets past module import.
"""

import os
import subprocess

import pytest_bazel

from util.bazel.runfiles import get_required_path

SERVER_BIN = "_main/finance/plaid/link/server_bin"


def test_container_entrypoint_imports_and_reaches_settings() -> None:
    """Run with no Plaid config: the binary must fail on *settings validation*, which it can only
    reach after module-level import succeeded. An import-time failure looks entirely different and
    is the regression this guards."""
    result = subprocess.run(
        [str(get_required_path(SERVER_BIN))],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
        # The nested binary needs the test's RUNFILES_* to find its own runfiles, so inherit the
        # environment and strip only the Plaid config — without it the binary cannot get as far as
        # binding a port.
        env={k: v for k, v in os.environ.items() if not k.startswith("PLAID_MCP_") and k != "DATABASE_URL"},
    )

    assert result.returncode != 0, "expected missing settings to fail the binary"
    assert "PlaidWebSettings" in result.stderr, (
        f"binary did not reach settings validation, so module import failed:\n{result.stderr}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
