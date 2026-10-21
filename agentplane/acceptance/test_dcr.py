"""Actions challenge -> SDK discovery -> RFC 7591 DCR, against the Action Service `ACTIONS_MCP_URL` names.

Registration only: no test operator credentials are read and no Action is executed.
"""

import os

import pytest
import pytest_bazel

from agentplane.acceptance.conftest import ACTIONS_MCP_URL
from agentplane.acceptance.dcr import DcrError, register_client


@pytest.mark.parametrize("redirect_uri", ["http://127.0.0.1:49152/callback", "https://client.example.test/callback"])
async def test_dynamic_client_registration(pytestconfig: pytest.Config, redirect_uri: str) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    try:
        await register_client(os.environ[ACTIONS_MCP_URL], redirect_uri)
    except DcrError as error:
        pytest.fail(str(error), pytrace=False)


if __name__ == "__main__":
    pytest_bazel.main()
