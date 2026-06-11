"""Prompt loading helpers for the twenty questions eval.

Loads shared prompt templates from text files via Bazel runfiles, providing a
single source of truth used by all implementations (Python, Rust, Go).
"""

from skills.eval_infra.eval_prompt import compose_system_prompt
from util.bazel.runfiles import get_required_path

_SIM_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/sim.txt"
_SCRATCH_NOTE_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/scratch_system_note.txt"
_FIRST_USER_MSG_RLOCATION = "_main/skills/info_gathering/evals/twenty_questions/first_user_message.txt"

_BASE_GUESSER_PREAMBLE = (
    "You are playing 20 Questions. Your goal is to identify the secret in as few questions as possible."
)


def load_sim_prompt(*, secret: str, turn_limit: int) -> str:
    template = get_required_path(_SIM_RLOCATION).read_text()
    return template.format(secret=secret, turn_limit=turn_limit)


def load_scratch_system_note() -> str:
    return get_required_path(_SCRATCH_NOTE_RLOCATION).read_text().strip()


def build_guesser_system(*, skill: str) -> str:
    """Compose the guesser's system prompt: preamble + skill block + exec note + game-tools note.

    The TQ guesser always has an `exec` container with the skill mounted at
    `SKILL_PATH` (`compose_system_prompt` appends the generic exec-tool
    description and the skill files-path note) plus the
    `ask_yes_no_question` / `guess_answer` game tools (this function appends
    `load_scratch_system_note()` as tool guidance).
    """
    return compose_system_prompt(
        preamble=_BASE_GUESSER_PREAMBLE, skill_md=skill, tool_guidance=load_scratch_system_note()
    )


def first_user_message(domain_description: str, turn_limit: int) -> str:
    template = get_required_path(_FIRST_USER_MSG_RLOCATION).read_text().strip()
    return template.format(domain_description=domain_description, turn_limit=turn_limit)
