"""Minimal reproduction of Final[str] cross-module type inference issue."""
from typing import Final

# These are Final[str] constants similar to adgn.mcp._shared.constants
APPROVAL_POLICY_RESOURCE_URI: Final[str] = "resource://approval-policy/policy.py"
COMPOSITOR_META_STATE_URI_FMT: Final[str] = "resource://compositor_meta/state/{server}"
DOCKER_SERVER_NAME: Final[str] = "docker"
