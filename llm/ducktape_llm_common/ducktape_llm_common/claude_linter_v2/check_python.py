"""Python file checking functionality."""

from .config.clean_models import ModularConfig
from .config.models import Violation
from .linters.python_ast import PythonASTAnalyzer
from .linters.python_ruff import PythonRuffLinter
from .pattern_matcher import PatternMatcher


def check_python_file(
    file_path: str, content: str, config: ModularConfig, critical_only: bool = False
) -> list[Violation]:
    """Check a Python file for violations.

    Args:
        file_path: Path to the file
        content: File content to check
        config: The configuration object
        critical_only: Whether to only return critical violations

    Returns:
        List of all violations found
    """
    violations = []

    # Get pattern-based context for this file
    pattern_matcher = PatternMatcher(config.pattern_rules)
    file_context = pattern_matcher.get_file_context(file_path)

    # Check if bare except should be enforced for this file
    bare_except_enabled = config.get_rule_config("python.bare_except")
    bare_except_enabled = bare_except_enabled.enabled if bare_except_enabled else True
    if "python.bare_except" in file_context["relaxed_checks"]:
        bare_except_enabled = False

    # Check if hasattr/getattr/setattr should be enforced
    hasattr_config = config.get_rule_config("python.hasattr")
    getattr_config = config.get_rule_config("python.getattr")
    setattr_config = config.get_rule_config("python.setattr")

    getattr_setattr_enabled = (
        (hasattr_config and hasattr_config.enabled)
        or (getattr_config and getattr_config.enabled)
        or (setattr_config and setattr_config.enabled)
    )

    # Check if barrel init should be enforced
    barrel_init_config = config.get_rule_config("python.barrel_init")
    barrel_init_enabled = barrel_init_config.enabled if barrel_init_config else True

    # Run AST checks
    analyzer = PythonASTAnalyzer(
        bare_except=bare_except_enabled,
        getattr_setattr=getattr_setattr_enabled,
        barrel_init=file_path.endswith("__init__.py") and barrel_init_enabled,
    )
    ast_violations = analyzer.analyze_code(content, file_path)

    # Filter out relaxed AST violations
    for v in ast_violations:
        check_name = f"AST:{v.rule}" if not v.rule.startswith("AST:") else v.rule
        should_relax, _ = pattern_matcher.should_relax_check(file_path, check_name)
        if not should_relax:
            violations.append(v)

    # Run ruff checks
    force_select = config.get_ruff_codes_to_select()
    linter = PythonRuffLinter(force_select=force_select)
    ruff_violations = linter.check_code(content, file_path, critical_only=critical_only)

    # Filter out relaxed ruff violations
    for v in ruff_violations:
        # Check both with and without ruff. prefix
        should_relax = False
        for check_name in [v.rule, f"ruff.{v.rule}"]:
            relax, _ = pattern_matcher.should_relax_check(file_path, check_name)
            if relax:
                should_relax = True
                break
        if not should_relax:
            violations.append(v)

    return violations
