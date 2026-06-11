"""Clean implementation of violation checking and filtering."""

from typing import Literal

from .config.clean_models import ModularConfig
from .config.models import Violation
from .rule_registry import RuleRegistry, map_violation_to_rule_key


def filter_violations(
    violations: list[Violation], config: ModularConfig, hook_type: Literal["pre", "stop"]
) -> list[Violation]:
    """Filter violations based on configuration and hook type.

    Args:
        violations: List of all violations found
        config: The configuration object
        hook_type: Which hook is filtering ("pre" for PreToolUse, "stop" for Stop)

    Returns:
        Only violations that should block for the given hook type
    """
    result = []

    for violation in violations:
        # Map violation to canonical rule key
        rule_key = map_violation_to_rule_key(violation)
        if not rule_key:
            # Unknown rule - don't block
            continue

        # Get rule definition from registry
        rule_def = RuleRegistry.get_by_key(rule_key)
        if not rule_def:
            # Not in registry - don't block
            continue

        # Get user configuration for this rule
        rule_config = config.get_rule_config(rule_key)

        # Check if enabled (default True if not configured)
        enabled = rule_config.enabled if rule_config else True
        if not enabled:
            continue

        # Determine if this blocks for the given hook type
        if hook_type == "pre":
            # Check blocks_pre_hook: user override or default
            if rule_config and rule_config.blocks_pre_hook is not None:
                blocks = rule_config.blocks_pre_hook
            else:
                blocks = rule_def.default_blocks_pre
        # Check blocks_stop_hook: user override or default
        elif rule_config and rule_config.blocks_stop_hook is not None:
            blocks = rule_config.blocks_stop_hook
        else:
            blocks = rule_def.default_blocks_stop

        if blocks:
            result.append(violation)

    return result
