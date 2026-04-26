"""Prompt loading helpers for the twenty questions eval.

Loads shared prompt templates from text files via Bazel runfiles, providing a
single source of truth used by all implementations (Python, Rust, Go).
"""

from pathlib import Path

from skills.eval_infra.eval_prompt import compose_system_prompt
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
    """Compose the guesser's system prompt: preamble + skill block + exec note + game-tools note.

    `skill_files_path=None` is for the four non-AF framework variants that
    don't mount the skill into a sandbox; AF rollouts always pass a real path.
    `has_scratch=True` adds the TQ-specific game-tools guidance (the generic
    exec-tool description is always appended by `compose_system_prompt`).
    """
    return compose_system_prompt(
        preamble=_BASE_GUESSER_PREAMBLE,
        skill_md=skill,
        skill_files_path=skill_files_path,
        tool_guidance=load_scratch_system_note() if has_scratch else None,
    )


def first_user_message(domain_description: str, turn_limit: int) -> str:
    template = get_required_path(_FIRST_USER_MSG_RLOCATION).read_text().strip()
    return template.format(domain_description=domain_description, turn_limit=turn_limit)
