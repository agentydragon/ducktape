"""Shared assembly of skill-eval system prompts.

`compose_system_prompt` joins four pieces with `\\n\\n---\\n\\n` separators:

1. An eval-specific preamble (what task is being performed).
2. A skill block — generic skill-intro line + inlined SKILL.md + a one-line
   pointer at the in-container skill mount. The skill is always mounted
   (the off-arm of a mounted-skill rollout uses the empty-skill tar; the
   inlined `<skill>` payload is empty but the path note still applies).
   The intro wording is fixed: any eval introducing any skill should
   phrase it the same way.
3. A generic `exec`-tool description — describes the sandbox container that
   every rollout exposes via `eval_sandbox` / `scratch_exec_server`. Always
   appended; every caller has an exec tool sandbox.
4. Optional eval-specific tool guidance (game tools, submit, etc.).

Used by TQ's `build_guesser_system`, FL's `build_system_prompt`, and RE's
`_build_system_prompt` to give every eval the same skill-block + exec-note
shape.
"""

from pathlib import Path

_SKILL_INTRO = "Follow this skill throughout the task."

_EXEC_TOOL_NOTE = (
    "You have shell access via the `exec` tool — `cmd` is a list of strings, no shell "
    "expansion. Stdout and stderr are returned together. The container is "
    "`python:3.13-slim` (Debian-based) with internet access; install whatever you need "
    "(`apt-get install -y ...`, `pip install ...`, `curl ...`)."
)


def compose_system_prompt(*, preamble: str, skill_md: str, skill_files_path: Path, tool_guidance: str | None) -> str:
    """See module docstring."""
    skill_block = (
        f"{_SKILL_INTRO}\n\n<skill>\n{skill_md}\n</skill>\n\n"
        f"The full skill (SKILL.md and any referenced example files) is available in the "
        f"container at {skill_files_path}/."
    )
    parts: list[str] = [preamble, skill_block, _EXEC_TOOL_NOTE]
    if tool_guidance is not None:
        parts.append(tool_guidance)
    return "\n\n---\n\n".join(parts)
