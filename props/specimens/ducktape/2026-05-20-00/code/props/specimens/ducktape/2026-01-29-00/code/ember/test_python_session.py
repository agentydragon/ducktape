from __future__ import annotations

import pytest
import pytest_bazel

from ember.python_session import ensure_kernel, restart_kernel, run_code


def test_persistent_python_session_preserves_state():
    conn = ensure_kernel()
    if conn is None:
        pytest.skip("ipykernel not available in runtime")

    assert conn.exists(), "kernel connection file should exist"

    run_code("x = 41")
    run_code("x += 1")
    output = run_code("print(x)")
    assert "42" in output

    restart_kernel()
    post_restart = run_code("print(globals().get('x'))")
    assert "None" in post_restart


if __name__ == "__main__":
    pytest_bazel.main()
