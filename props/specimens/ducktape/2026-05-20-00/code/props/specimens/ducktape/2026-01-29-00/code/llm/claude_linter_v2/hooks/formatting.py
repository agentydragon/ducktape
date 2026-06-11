"""Format messages for Claude using Python's string.Template."""

from string import Template

from llm.claude_code_api import SessionID
from llm.claude_linter_v2.config.models import Violation

# Templates for different message types
ACCESS_DENIAL_TEMPLATE = Template("""$message

To request an override, ask the user to run:
cl2 session allow '$predicate' --session $session_id""")

VIOLATIONS_TEMPLATE = Template("""$summary
$violations
$extra_info""")

QUALITY_GATE_TEMPLATE = Template("""Cannot end turn: $total code quality issues remain:

By severity:
$severity_list

By file:
$file_list

Please fix these issues before ending your turn.
Run 'cl2 check' locally to verify all issues are resolved.""")


def format_violations_list(violations: list[Violation], max_show: int = 3) -> str:
    """Format a list of violations."""
    lines = []
    for v in violations[:max_show]:
        lines.append(f"Line {v.line}: {v.message}")

    if len(violations) > max_show:
        remaining = len(violations) - max_show
        lines.append(f"... and {remaining} more violation{'s' if remaining > 1 else ''}")

    return "\n".join(lines)


def format_access_denial(predicate: str, session_id: SessionID, message: str | None = None) -> str:
    """Format access control denial."""
    return ACCESS_DENIAL_TEMPLATE.substitute(
        message=message or "Permission denied", predicate=predicate, session_id=session_id
    )


def format_quality_gate_failure(by_severity: dict[str, int], by_file: dict[str, int]) -> str:
    """Format quality gate failure."""
    # Build severity list
    severity_lines = []
    if by_severity.get("error", 0) > 0:
        severity_lines.append(f"- {by_severity['error']} errors (must fix)")
    if by_severity.get("warning", 0) > 0:
        severity_lines.append(f"- {by_severity['warning']} warnings")
    if by_severity.get("info", 0) > 0:
        severity_lines.append(f"- {by_severity['info']} info")

    # Build file list (top 5)
    file_lines = []
    for path, count in sorted(by_file.items())[:5]:
        file_lines.append(f"- {path}: {count} issues")
    if len(by_file) > 5:
        file_lines.append(f"- ... and {len(by_file) - 5} more files")

    return QUALITY_GATE_TEMPLATE.substitute(
        total=sum(by_severity.values()), severity_list="\n".join(severity_lines), file_list="\n".join(file_lines)
    )


def format_llm_message(summary: str, violations: str, extra_info: str = "") -> str:
    """Format a message to Claude about violations."""
    return VIOLATIONS_TEMPLATE.substitute(summary=summary, violations=violations, extra_info=extra_info)
