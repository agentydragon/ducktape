"""Shared assembly of skill-eval system prompts.

`compose_system_prompt` joins three pieces with `\\n\\n---\\n\\n` separators:

1. An eval-specific preamble (what game / task is being played).
2. A skill block — inlined SKILL.md plus a one-line pointer at the in-container
   skill mount. Included whenever `skill_md` is non-empty or `skill_files_path`
   is not None. The off-arm of a mounted-skill rollout passes empty `skill_md`
   and a real `skill_files_path`, so the block stays present (consistent
   sandbox shape) but the inlined `<skill>` payload is empty.
3. An optional eval-specific scratch-tool note.

TQ's `build_guesser_system` and FL's `build_system_prompt` are thin wrappers
around this. RE's prompt has a different structural shape and does not use it.
"""

from pathlib import Path

_SKILL_INTRO = "Follow this information-gathering skill throughout."


def compose_system_prompt(
    *,
    preamble: str,
    skill_md: str,
    skill_files_path: Path | None,
    scratch_note: str | None,
    skill_intro: str = _SKILL_INTRO,
) -> str:
    """See module docstring."""
    parts: list[str] = [preamble]
    if skill_md or skill_files_path is not None:
        skill_block = f"{skill_intro}\n\n<skill>\n{skill_md}\n</skill>"
        if skill_files_path is not None:
            skill_block += (
                f"\n\nThe full skill (SKILL.md and any referenced example files) is "
                f"available in the container at {skill_files_path}/."
            )
        parts.append(skill_block)
    if scratch_note is not None:
        parts.append(scratch_note)
    return "\n\n---\n\n".join(parts)
