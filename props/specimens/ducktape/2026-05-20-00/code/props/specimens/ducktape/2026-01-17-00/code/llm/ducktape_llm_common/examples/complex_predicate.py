"""Example of adding a complex predicate for shell pipeline safety checking."""

import shlex

from ducktape_llm_common.claude_linter_v2.access.context import PredicateContext
from ducktape_llm_common.claude_linter_v2.access.predicates import register_builtin


def safe_shell_pipeline(ctx: PredicateContext) -> bool:
    """
    Check if a shell pipeline only consists of known safe commands with known safe flags.

    This is a complex multiline function that analyzes shell commands.
    """
    if ctx.tool != "Bash":
        return False

    if not ctx.command:
        return False

    # Define safe commands and their allowed flags
    safe_commands = {
        "grep": {"-r", "-i", "-n", "-v", "-E", "-F", "-l", "-c", "--include", "--exclude"},
        "find": {"-name", "-type", "-path", "-maxdepth", "-mindepth", "-exec", "-print"},
        "ls": {"-l", "-a", "-h", "-R", "-t", "-S", "-r", "-1"},
        "cat": set(),  # No flags needed
        "head": {"-n", "-c"},
        "tail": {"-n", "-f", "-F"},
        "wc": {"-l", "-w", "-c"},
        "sort": {"-n", "-r", "-u", "-k"},
        "uniq": {"-c", "-d", "-u"},
        "cut": {"-d", "-f", "-c"},
        "awk": set(),  # Too complex to validate here
        "sed": set(),  # Too complex to validate here
        "echo": set(),
        "printf": set(),
    }

    # Parse the pipeline
    try:
        # Split by pipe
        pipeline_parts = ctx.command.split("|")

        for part_raw in pipeline_parts:
            part = part_raw.strip()
            if not part:
                continue

            # Parse the command
            try:
                tokens = shlex.split(part)
            except ValueError:
                # Malformed command
                return False

            if not tokens:
                continue

            cmd = tokens[0]

            # Check if command is in safe list
            if cmd not in safe_commands:
                return False

            # Check flags (simple check - just look for strings starting with -)
            allowed_flags = safe_commands[cmd]
            for token in tokens[1:]:
                if token.startswith("-") and token not in allowed_flags:
                    # Unknown flag
                    return False

        return True

    except (ValueError, TypeError, AttributeError):
        # Any parsing error means unsafe
        return False


# Register this predicate so it can be used in config
register_builtin("safe_shell_pipeline", safe_shell_pipeline)
