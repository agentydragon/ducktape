"""Central registry of all linting rules.

This is the single source of truth for all rules in the system.
No string parsing or field name manipulation should be used to determine rule properties.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from llm.claude_linter_v2.config.models import Violation


@dataclass(frozen=True)
class RuleDefinition:
    """Definition of a single linting rule."""

    code: str  # The actual rule code (e.g., "E722", "bare_except")
    category: str  # Category (e.g., "python", "ruff")
    default_blocks_pre: bool  # Should block PreToolUse by default?
    default_blocks_stop: bool  # Should block Stop hook by default?
    default_message: str  # Default error message
    description: str  # Human-readable description of what the rule checks


class RuleRegistry:
    """Central registry of all available rules."""

    # Python AST-based rules
    PYTHON_BARE_EXCEPT = RuleDefinition(
        code="bare_except",
        category="python",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Bare except clauses hide errors and make debugging impossible",
        description="Detects bare 'except:' without exception type",
    )

    PYTHON_HASATTR = RuleDefinition(
        code="hasattr",
        category="python",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="hasattr() circumvents type safety - use proper type checking",
        description="Detects usage of hasattr() function",
    )

    PYTHON_GETATTR = RuleDefinition(
        code="getattr",
        category="python",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="getattr() circumvents type safety - use proper attribute access",
        description="Detects usage of getattr() function",
    )

    PYTHON_SETATTR = RuleDefinition(
        code="setattr",
        category="python",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="setattr() circumvents type safety - use proper attribute assignment",
        description="Detects usage of setattr() function",
    )

    PYTHON_BARREL_INIT = RuleDefinition(
        code="barrel_init",
        category="python",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Barrel __init__.py files create circular import risks",
        description="Detects __init__.py files that import and re-export everything",
    )

    # Ruff rules - Critical/Security
    RUFF_E722 = RuleDefinition(
        code="E722",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use bare except - specify exception types",
        description="bare-except",
    )

    RUFF_BLE001 = RuleDefinition(
        code="BLE001",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use blind exception handling",
        description="blind-except",
    )

    RUFF_B009 = RuleDefinition(
        code="B009",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use getattr with a constant - use normal attribute access",
        description="getattr-with-constant",
    )

    RUFF_B010 = RuleDefinition(
        code="B010",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use setattr with a constant - use normal attribute assignment",
        description="setattr-with-constant",
    )

    RUFF_S113 = RuleDefinition(
        code="S113",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Requests without timeout can hang indefinitely",
        description="request-without-timeout",
    )

    RUFF_S608 = RuleDefinition(
        code="S608",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Possible SQL injection - use parameterized queries",
        description="hardcoded-sql-expression",
    )

    RUFF_B006 = RuleDefinition(
        code="B006",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use mutable default arguments",
        description="mutable-argument-default",
    )

    RUFF_PGH003 = RuleDefinition(
        code="PGH003",
        category="ruff",
        default_blocks_pre=True,
        default_blocks_stop=True,
        default_message="Do not use blanket type: ignore - it defeats type checking",
        description="blanket-type-ignore",
    )

    # Ruff rules - Style/Non-critical
    RUFF_E501 = RuleDefinition(
        code="E501",
        category="ruff",
        default_blocks_pre=False,
        default_blocks_stop=False,
        default_message="Line exceeds maximum length",
        description="line-too-long",
    )

    RUFF_B008 = RuleDefinition(
        code="B008",
        category="ruff",
        default_blocks_pre=False,
        default_blocks_stop=False,
        default_message="Do not use function calls in default arguments",
        description="function-call-in-default-argument",
    )

    RUFF_E402 = RuleDefinition(
        code="E402",
        category="ruff",
        default_blocks_pre=False,
        default_blocks_stop=False,
        default_message="Module level import not at top of file",
        description="module-import-not-at-top-of-file",
    )

    RUFF_PLC0415 = RuleDefinition(
        code="PLC0415",
        category="ruff",
        default_blocks_pre=False,
        default_blocks_stop=False,
        default_message="Import should be at module level",
        description="import-outside-top-level",
    )

    RUFF_B904 = RuleDefinition(
        code="B904",
        category="ruff",
        default_blocks_pre=False,
        default_blocks_stop=False,
        default_message="Use 'raise ... from' inside except blocks",
        description="raise-without-from-inside-except",
    )

    # Create lookup tables
    _BY_KEY: ClassVar[dict[str, RuleDefinition]] = {}
    _BY_CODE: ClassVar[dict[tuple[str, str], RuleDefinition]] = {}
    _INITIALIZED = False

    @classmethod
    def _initialize(cls) -> None:
        """Initialize lookup tables from class attributes."""
        if cls._INITIALIZED:  # Already initialized
            return

        # Explicitly register all rules instead of using getattr
        all_rules = [
            cls.PYTHON_BARE_EXCEPT,
            cls.PYTHON_HASATTR,
            cls.PYTHON_GETATTR,
            cls.PYTHON_SETATTR,
            cls.PYTHON_BARREL_INIT,
            cls.RUFF_E722,
            cls.RUFF_BLE001,
            cls.RUFF_B009,
            cls.RUFF_B010,
            cls.RUFF_S113,
            cls.RUFF_S608,
            cls.RUFF_B006,
            cls.RUFF_PGH003,
            cls.RUFF_E501,
            cls.RUFF_B008,
            cls.RUFF_E402,
            cls.RUFF_PLC0415,
            cls.RUFF_B904,
        ]

        for rule in all_rules:
            # Create canonical key
            key = f"{rule.category}.{rule.code}"
            cls._BY_KEY[key] = rule
            cls._BY_CODE[(rule.category, rule.code)] = rule

        cls._INITIALIZED = True

    @classmethod
    def get_by_key(cls, key: str) -> RuleDefinition | None:
        """Get rule by canonical key (e.g., 'python.bare_except', 'ruff.E722')."""
        cls._initialize()
        return cls._BY_KEY.get(key)

    @classmethod
    def get_all_rules(cls) -> list[RuleDefinition]:
        """Get all registered rules."""
        cls._initialize()
        return list(cls._BY_KEY.values())

    @classmethod
    def get_by_code(cls, category: str, code: str) -> RuleDefinition | None:
        """Get rule by category and code."""
        cls._initialize()
        return cls._BY_CODE.get((category, code))

    @classmethod
    def get_all_keys(cls) -> list[str]:
        """Get all registered rule keys."""
        cls._initialize()
        return sorted(cls._BY_KEY.keys())

    @classmethod
    def get_ruff_codes(cls) -> list[str]:
        """Get all ruff rule codes that are enabled by default."""
        cls._initialize()
        return [rule.code for rule in cls._BY_KEY.values() if rule.category == "ruff"]


def map_violation_to_rule_key(violation: "Violation") -> str | None:
    """Map a violation to its canonical rule key.

    Args:
        violation: A Violation object with a 'rule' attribute

    Returns:
        Canonical rule key (e.g., 'python.bare_except') or None if unknown
    """
    rule = violation.rule

    # Handle ruff rules (format: "ruff:CODE")
    if rule.startswith("ruff:"):
        code = rule[5:]  # Remove "ruff:" prefix
        return f"ruff.{code}"

    # Handle python AST rules (format: "bare_except", "getattr_setattr", etc.)
    if rule in ["bare_except", "barrel_init"]:
        return f"python.{rule}"

    # Special case: getattr_setattr rule reports individual violations
    if rule == "getattr_setattr":
        # Check the message to determine which specific rule
        if "hasattr" in violation.message:
            return "python.hasattr"
        if "getattr" in violation.message:
            return "python.getattr"
        if "setattr" in violation.message:
            return "python.setattr"

    return None
