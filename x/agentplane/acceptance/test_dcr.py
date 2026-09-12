"""Live testing ingress -> Actions challenge -> SDK discovery -> RFC 7591 DCR.

Registration only: no test operator credentials are read and no Action is executed.
"""

import pytest
import pytest_bazel

from x.agentplane.acceptance.dcr import DcrError, register_client

TESTING_MCP = "https://agentplane-actions-testing.allegedly.works/mcp"


@pytest.mark.parametrize("redirect_uri", ["http://127.0.0.1:49152/callback", "https://client.example.test/callback"])
async def test_dynamic_client_registration(pytestconfig: pytest.Config, redirect_uri: str) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    try:
        await register_client(TESTING_MCP, redirect_uri)
    except DcrError as error:
        pytest.fail(str(error), pytrace=False)


if __name__ == "__main__":
    pytest_bazel.main()
