"""PreToolUse hook: programmatic permission predicate.

Evaluates tool calls and returns allow/deny/ask decisions before Claude Code's
normal permission system.
"""

from devinfra.claude.claude_api.hooks.pre_tool_use import (
    PermissionDecision,
    PreToolUseHookSpecificOutput,
    PreToolUseInput,
    PreToolUseOutput,
)

# --- Config ---

ALWAYS_ALLOW_COMMANDS: set[str] = {"echo hello world"}


def evaluate(hook_input: PreToolUseInput) -> PreToolUseOutput:
    """Evaluate a tool call against permission policies."""
    if hook_input.tool_name == "Bash":
        command = hook_input.tool_input.get("command", "")
        if command in ALWAYS_ALLOW_COMMANDS:
            return PreToolUseOutput(
                hook_specific_output=PreToolUseHookSpecificOutput(
                    permission_decision=PermissionDecision.ALLOW,
                    permission_decision_reason="Command in always-allow list",
                )
            )
    return PreToolUseOutput()
