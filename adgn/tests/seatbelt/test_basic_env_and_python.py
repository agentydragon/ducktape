from __future__ import annotations

import os
import sys

import pytest

from adgn.seatbelt.runner import run_sandboxed_async
from tests._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC]


@pytest.mark.parametrize(
    ("cmd", "set_env", "passthrough", "expect_substring"),
    [
        (["/bin/sh", "-c", "yes hello | head -n 3"], {"PYTHONUNBUFFERED": "1"}, [], b"hello"),
        ([sys.executable, "-c", "print('HELLO_VENV')"], {}, ["PATH", "PYTHONPATH"], b"HELLO_VENV"),
    ],
)
async def test_basic_env_and_python(allow_all_policy, cmd, set_env, passthrough, expect_substring):
    # Build env passthrough + set
    env = {k: os.environ[k] for k in passthrough if k in os.environ}
    env.update(set_env)

    res = await run_sandboxed_async(allow_all_policy, cmd, env=env, trace=True)
    assert res.exit_code == 0
    out = res.stdout or b""
    assert expect_substring in out
