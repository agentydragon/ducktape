# Shared constants for MCP mcp/_shared modules

from signal import SIGKILL, SIGTERM
from typing import Final
from urllib.parse import urlunparse

SLEEP_FOREVER_CMD: Final[list[str]] = ["/bin/sh", "-lc", "sleep infinity"]

# Canonical server/tool names for the agent runtime Docker MCP server
RUNTIME_SERVER_NAME: Final[str] = "runtime"
RUNTIME_EXEC_TOOL_NAME: Final[str] = "exec"
RUNTIME_CONTAINER_INFO_URI: Final[str] = "resource://container.info"

SIGNAL_EXIT_OFFSET: Final[int] = 128


def signal_exit_code(sig: int) -> int:
    return SIGNAL_EXIT_OFFSET + int(sig)


EXIT_CODE_SIGTERM: Final[int] = signal_exit_code(SIGTERM)
EXIT_CODE_SIGKILL: Final[int] = signal_exit_code(SIGKILL)

# Common server names
CRITIC_SUBMIT_SERVER_NAME: Final[str] = "critic_submit"
MATRIX_CONTROL_SERVER_NAME: Final[str] = "matrix_control"
UI_SERVER_NAME: Final[str] = "ui"
APPROVAL_POLICY_SERVER_NAME: Final[str] = "approval_policy"
APPROVAL_POLICY_SERVER_NAME_READER: Final[str] = APPROVAL_POLICY_SERVER_NAME
APPROVAL_POLICY_SERVER_NAME_PROPOSER: Final[str] = f"{APPROVAL_POLICY_SERVER_NAME}.proposer"
APPROVAL_POLICY_SERVER_NAME_APPROVER: Final[str] = f"{APPROVAL_POLICY_SERVER_NAME}.approver"
DOCKER_SERVER_NAME: Final[str] = "docker"
PROMPT_EVAL_SERVER_NAME: Final[str] = "prompt_eval"
EDITOR_SERVER_NAME: Final[str] = "editor"
SUBMIT_COMMIT_MESSAGE_SERVER_NAME: Final[str] = "submit_commit_message"
LINT_SUBMIT_SERVER_NAME: Final[str] = "lint_submit"
GRADER_SUBMIT_SERVER_NAME: Final[str] = "grader_submit"
RESOURCES_SERVER_NAME: Final[str] = "resources"
SEATBELT_EXEC_SERVER_NAME: Final[str] = "seatbelt_exec"

# Approval policy resource URI (neutral/logical; no host mount implications)
APPROVAL_POLICY_RESOURCE_URI: Final[str] = "resource://approval-policy/policy.py"
APPROVAL_POLICY_PROPOSALS_INDEX_URI: Final[str] = "resource://approval-policy/proposals"

# MCP notification method names (match MCP spec)
RESOURCES_UPDATED_METHOD: Final[str] = "notifications/resources/updated"
RESOURCES_LIST_CHANGED_METHOD: Final[str] = "notifications/resources/list_changed"

# Loopback/HTTP defaults (auth + embed)
LOOPBACK_HOST: Final[str] = "127.0.0.1"
DEFAULT_AUTH_ISSUER_URL: Final[str] = urlunparse(("http", LOOPBACK_HOST, "", "", "", ""))
DEFAULT_RESOURCE_SERVER_URL: Final[str] = urlunparse(("http", LOOPBACK_HOST, "", "", "", ""))

# Reserved JSON-RPC error codes for policy gateway denials
POLICY_DENIED_ABORT_CODE: Final[int] = -32950
POLICY_DENIED_CONTINUE_CODE: Final[int] = -32951

# Reserved JSON-RPC error code for policy evaluator failures
# Used when the approval policy evaluator itself errors or times out
POLICY_EVALUATOR_ERROR_CODE: Final[int] = -32953

# Canonical mapping for reserved-code misuse by backends
# Backends must not emit reserved policy denial codes/messages; the middleware
# remaps such attempts to this explicit error to prevent spoofing.
POLICY_BACKEND_RESERVED_MISUSE_CODE: Final[int] = -32952
POLICY_BACKEND_RESERVED_MISUSE_MSG: Final[str] = "policy_backend_reserved_misuse"

# Reserved JSON-RPC error messages for policy gateway denials
POLICY_DENIED_ABORT_MSG: Final[str] = "policy_denied"
POLICY_DENIED_CONTINUE_MSG: Final[str] = "policy_denied_continue"

# Reserved JSON-RPC error message for policy evaluator failures
POLICY_EVALUATOR_ERROR_MSG: Final[str] = "policy_evaluator_error"

# Compositor metadata server and resource URI templates (mounted under compositor)
COMPOSITOR_META_SERVER_NAME: Final[str] = "compositor_meta"
COMPOSITOR_META_URI_PREFIX: Final[str] = "resource://compositor_meta"
COMPOSITOR_META_STATE_URI_FMT: Final[str] = f"{COMPOSITOR_META_URI_PREFIX}/state/{{server}}"
COMPOSITOR_META_INSTRUCTIONS_URI_FMT: Final[str] = (
    f"{COMPOSITOR_META_URI_PREFIX}/instructions/{{server}}"
)
COMPOSITOR_META_CAPABILITIES_URI_FMT: Final[str] = (
    f"{COMPOSITOR_META_URI_PREFIX}/capabilities/{{server}}"
)

# Compositor admin server name
COMPOSITOR_ADMIN_SERVER_NAME: Final[str] = "compositor_admin"

# Subscriptions index (aggregated by resources server)
RESOURCES_SUBSCRIPTIONS_INDEX_URI: Final[str] = "resources://subscriptions"

# Policy Gateway stamping key placed on error.data to unambiguously mark origin
POLICY_GATEWAY_STAMP_KEY: Final[str] = "adgn_policy_gateway"
