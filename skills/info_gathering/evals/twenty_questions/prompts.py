"""Prompt loading helpers for the twenty questions eval.

Loads shared prompt templates from text files via Bazel runfiles, providing a
single source of truth used by all implementations (Python, Rust, Go).
"""

from pathlib import Path

from util.bazel.runfiles import get_required_path

_SIM_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/sim.txt"
_SKILL_RLOCATION = "_main/skills/info_gathering/SKILL.md"
_SCRATCH_NOTE_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/scratch_system_note.txt"
_FIRST_USER_MSG_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/first_user_message.txt"

_BASE_GUESSER_PREAMBLE = (
    "You are playing 20 Questions. Your goal is to identify the secret in as few questions as possible."
)


def load_sim_prompt(*, secret: str, turn_limit: int) -> str:
    template = get_required_path(_SIM_RLOCATION).read_text()
    return template.format(secret=secret, turn_limit=turn_limit)


def load_skill_prompt() -> str:
    return get_required_path(_SKILL_RLOCATION).read_text()


def load_scratch_system_note() -> str:
    return get_required_path(_SCRATCH_NOTE_RLOCATION).read_text().strip()


def build_guesser_system(*, skill: str, has_scratch: bool, skill_files_path: Path | None) -> str:
    """Compose the guesser's system prompt from independent pieces.

    Args:
        skill: Skill text to inline. Empty string for the off-arm of a
               mounted-skill rollout (the empty-skill tar's SKILL.md is blank).
        has_scratch: Include the scratch container exec tool note.
        skill_files_path: In-container path where the skill tar is bind-mounted
                          (e.g. ``/work/.skill``), or ``None`` when the rollout
                          doesn't mount the skill into a sandbox (the four
                          non-AF framework variants today). Pass it explicitly.
    """
    parts: list[str] = [_BASE_GUESSER_PREAMBLE]
    if skill or skill_files_path is not None:
        skill_block = f"Follow this information-gathering skill throughout.\n\n<skill>\n{skill}\n</skill>"
        if skill_files_path is not None:
            skill_block += (
                f"\n\nThe full skill (SKILL.md and any referenced example files) is available "
                f"in the container at {skill_files_path}/."
            )
        parts.append(skill_block)
    if has_scratch:
        parts.append(load_scratch_system_note())
    return "\n\n---\n\n".join(parts)


def first_user_message(domain_description: str, turn_limit: int) -> str:
    template = get_required_path(_FIRST_USER_MSG_RLOCATION).read_text().strip()
    return template.format(domain_description=domain_description, turn_limit=turn_limit)
