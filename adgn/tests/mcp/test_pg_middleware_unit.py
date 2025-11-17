from mcp import McpError, types
import pytest

from adgn.mcp._shared.constants import (
    POLICY_BACKEND_RESERVED_MISUSE_CODE,
    POLICY_BACKEND_RESERVED_MISUSE_MSG,
    POLICY_GATEWAY_STAMP_KEY,
)
from adgn.mcp.policy_gateway.middleware import _raise_if_reserved_code


async def test_raise_if_reserved_code_remaps_stamped_upstream() -> None:
    # Simulate a downstream server raising McpError with a spoofed gateway stamp
    e = McpError(
        types.ErrorData(code=-32000, message="upstream_error", data={POLICY_GATEWAY_STAMP_KEY: True, "note": "spoof"})
    )

    with pytest.raises(McpError) as ei:
        _raise_if_reserved_code(e, name="backend__raise")

    err = ei.value
    # The middleware should remap to explicit backend misuse
    assert getattr(err, "error", None) is not None
    assert err.error.code == POLICY_BACKEND_RESERVED_MISUSE_CODE
    assert err.error.message == POLICY_BACKEND_RESERVED_MISUSE_MSG
