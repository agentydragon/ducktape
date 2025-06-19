"""Common utilities for team tools."""

import sys


def error_exit(message: str) -> None:
    """Print error message with help pointer and exit."""
    print(f"❌ {message}")
    print("\nFor full instructions, see: ~/.claude/commands/agent-boot.md")
    sys.exit(1)
