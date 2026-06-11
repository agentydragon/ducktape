from __future__ import annotations

import platform

import pytest

from ._markers import REQUIRES_SANDBOX_EXEC

pytestmark = [*REQUIRES_SANDBOX_EXEC, pytest.mark.shell]


@pytest.fixture
def require_sandbox_exec():
    """Gate shell sandbox tests to supported platforms."""
    if platform.system() != "Darwin":
        pytest.skip("sandboxer tests require macOS host")
    return True
