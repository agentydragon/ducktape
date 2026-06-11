"""Rule evaluation engine with precedence and 'most restrictive wins' logic."""

import logging
from dataclasses import dataclass
from enum import IntEnum

from ..config.clean_models import ModularConfig
from ..config.models import AccessControlRule, RuleAction
from ..session.manager import SessionManager
from ..types import SessionID
from .context import PredicateContext
from .evaluator import PredicateEvaluator

logger = logging.getLogger(__name__)


class RuleSource(IntEnum):
    """Source of a rule, used for precedence."""

    CONFIG_ACCESS_CONTROL = 1  # Lowest precedence
    CONFIG_REPO_RULES = 2
    SESSION_RULES = 3  # Highest precedence


@dataclass
class RuleMatch:
    """A matched rule with its source and action."""

    action: RuleAction
    source: RuleSource
    message: str | None = None
    rule_description: str | None = None


class RuleEngine:
    """Evaluates access control rules with proper precedence."""

    def __init__(self, config: ModularConfig, session_manager: SessionManager) -> None:
        """
        Initialize the rule engine.

        Args:
            config: Linter configuration
            session_manager: Session manager for session rules
        """
        self.config = config
        self.session_manager = session_manager
        self.evaluator = PredicateEvaluator()

    def evaluate_access(self, context: PredicateContext, session_id: SessionID) -> tuple[RuleAction, str | None]:
        """
        Evaluate all applicable rules for the given context.

        Uses "most restrictive wins" logic:
        - DENY > WARN > ALLOW
        - Higher precedence rules override lower precedence

        Args:
            context: Predicate context
            session_id: Session ID for session rules

        Returns:
            Tuple of (action, message)
        """
        matches: list[RuleMatch] = []

        # 1. Check path-based access control rules (lowest precedence)
        for access_rule in self.config.access_control:
            if self._match_access_control_rule(access_rule, context):
                matches.append(
                    RuleMatch(
                        action=access_rule.action,
                        source=RuleSource.CONFIG_ACCESS_CONTROL,
                        message=access_rule.message,
                        rule_description=f"Path rule: {', '.join(access_rule.paths)}",
                    )
                )

        # 2. Check repo-wide predicate rules
        for predicate_rule in self.config.repo_rules:
            if self.evaluator.evaluate(predicate_rule.predicate, context):
                matches.append(
                    RuleMatch(
                        action=predicate_rule.action,
                        source=RuleSource.CONFIG_REPO_RULES,
                        message=predicate_rule.message,
                        rule_description=f"Repo rule: {predicate_rule.predicate}",
                    )
                )

        # 3. Check session-specific rules (highest precedence)
        session_rules = self.session_manager.get_session_rules(session_id)
        for session_rule in session_rules:
            predicate = session_rule.predicate
            if self.evaluator.evaluate(predicate, context):
                action = RuleAction(session_rule.action)
                matches.append(
                    RuleMatch(
                        action=action,
                        source=RuleSource.SESSION_RULES,
                        message=None,
                        rule_description=f"Session rule: {predicate}",
                    )
                )

        # Apply "most restrictive wins" logic
        if not matches:
            # No rules matched, default to allow
            return RuleAction.ALLOW, None

        # Sort by:
        # 1. Action severity (DENY > WARN > ALLOW)
        # 2. Source precedence (SESSION > REPO > ACCESS_CONTROL)
        def rule_sort_key(match: RuleMatch) -> tuple[int, int]:
            # Action severity (lower number = more restrictive)
            action_severity = {RuleAction.DENY: 0, RuleAction.WARN: 1, RuleAction.ALLOW: 2}
            return (action_severity[match.action], -match.source.value)

        matches.sort(key=rule_sort_key)

        # Take the most restrictive rule
        winner = matches[0]

        # Build message
        message_parts = []
        if winner.message:
            message_parts.append(winner.message)
        # Default messages
        elif winner.action == RuleAction.DENY:
            message_parts.append("Permission denied")
        elif winner.action == RuleAction.WARN:
            message_parts.append("Warning")

        if winner.rule_description:
            message_parts.append(f"({winner.rule_description})")

        # Add information about overridden rules if relevant
        if len(matches) > 1 and winner.action == RuleAction.DENY:
            # Count how many allows were overridden
            allow_count = sum(1 for m in matches if m.action == RuleAction.ALLOW)
            if allow_count > 0:
                message_parts.append(f"Note: {allow_count} allow rule(s) were overridden by this deny rule")

        message = ". ".join(message_parts) if message_parts else None

        logger.debug(
            f"Rule evaluation for {context}: "
            f"winner={winner.action} from {winner.source.name}, "
            f"total matches={len(matches)}"
        )

        return winner.action, message

    def _match_access_control_rule(self, rule: AccessControlRule, context: PredicateContext) -> bool:
        """
        Check if an access control rule matches the context.

        Args:
            rule: Access control rule
            context: Predicate context

        Returns:
            True if rule matches
        """
        # Check if tool matches
        if context.tool not in rule.tools:
            return False

        # Check if path matches any pattern
        if not context.path:
            return False

        return any(context.glob_match(pattern) for pattern in rule.paths)
