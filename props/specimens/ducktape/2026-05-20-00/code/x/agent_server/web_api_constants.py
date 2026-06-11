"""URI constants exported to the TypeScript frontend via code generation.

All module-level string constants whose names contain 'URI' are picked up by
generate_frontend_code.py and emitted as TypeScript constants/helpers.
"""

from typing import Final

from mcp_infra.compositor.resources_server import ResourcesServer
from mcp_infra.exec.docker.server import ContainerExecServer
from x.agent_server.mcp.approval_policy.engine import PolicyReaderServer

APPROVAL_POLICY_PROPOSALS_INDEX_URI: Final[str] = "resource://approval-policy/proposals"
APPROVAL_POLICY_RESOURCE_URI: Final[str] = PolicyReaderServer.ACTIVE_POLICY_URI
PENDING_CALLS_URI: Final[str] = PolicyReaderServer.PENDING_CALLS_URI
RESOURCES_SUBSCRIPTIONS_INDEX_URI: Final[str] = ResourcesServer.SUBSCRIPTIONS_INDEX_URI
RUNTIME_CONTAINER_INFO_URI: Final[str] = ContainerExecServer.CONTAINER_INFO_URI

# Format strings — the {param} placeholders become TypeScript function parameters.
APPROVAL_POLICY_PROPOSAL_ITEM_URI_FMT: Final[str] = PolicyReaderServer.PROPOSAL_ITEM_URI_TEMPLATE
