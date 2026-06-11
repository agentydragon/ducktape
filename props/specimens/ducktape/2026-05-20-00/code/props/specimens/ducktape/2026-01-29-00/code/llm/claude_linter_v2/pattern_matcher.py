"""Pattern-based rule matching for files."""

import fnmatch
from pathlib import Path

from llm.claude_linter_v2.config.models import PatternBasedRule


class PatternMatcher:
    """Matches files against pattern-based rules."""

    def __init__(self, pattern_rules: list[PatternBasedRule]) -> None:
        """Initialize with pattern rules."""
        self.pattern_rules = [rule for rule in pattern_rules if rule.enabled]

    def get_applicable_rules(self, file_path: str | Path) -> list[PatternBasedRule]:
        """Get all rules that apply to a given file path.

        Args:
            file_path: Path to check

        Returns:
            List of applicable rules (may be empty)
        """
        file_path = str(file_path)
        applicable_rules = []

        for rule in self.pattern_rules:
            for pattern in rule.patterns:
                if fnmatch.fnmatch(file_path, pattern):
                    applicable_rules.append(rule)
                    break  # Don't need to check other patterns for this rule

        return applicable_rules

    def should_relax_check(self, file_path: str | Path, check_name: str) -> tuple[bool, str | None]:
        """Check if a specific check should be relaxed for a file.

        Args:
            file_path: Path to check
            check_name: Name of the check (e.g., "python.bare_except")

        Returns:
            Tuple of (should_relax, custom_message)
        """
        applicable_rules = self.get_applicable_rules(file_path)

        for rule in applicable_rules:
            # Special case: "ALL" relaxes everything
            if "ALL" in rule.relaxed_checks:
                return True, rule.custom_message
            if check_name in rule.relaxed_checks:
                return True, rule.custom_message

        return False, None

    def should_enforce_check(self, file_path: str | Path, check_name: str) -> tuple[bool, str | None]:
        """Check if a specific check should be enforced for a file.

        Args:
            file_path: Path to check
            check_name: Name of the check

        Returns:
            Tuple of (should_enforce, custom_message)
        """
        applicable_rules = self.get_applicable_rules(file_path)

        for rule in applicable_rules:
            if check_name in rule.enforced_checks:
                return True, rule.custom_message

        return False, None

    def get_file_context(self, file_path: str | Path) -> dict[str, list[str]]:
        """Get complete context for a file.

        Args:
            file_path: Path to check

        Returns:
            Dict with 'relaxed_checks', 'enforced_checks', and 'rule_names'
        """
        applicable_rules = self.get_applicable_rules(file_path)

        context: dict[str, list[str]] = {"relaxed_checks": [], "enforced_checks": [], "rule_names": []}

        for rule in applicable_rules:
            context["relaxed_checks"].extend(rule.relaxed_checks)
            context["enforced_checks"].extend(rule.enforced_checks)
            context["rule_names"].append(rule.name)

        # Remove duplicates while preserving order
        context["relaxed_checks"] = list(dict.fromkeys(context["relaxed_checks"]))
        context["enforced_checks"] = list(dict.fromkeys(context["enforced_checks"]))

        return context
