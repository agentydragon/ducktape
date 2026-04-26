"""Prompt loading helpers for the function learning eval."""

from pathlib import Path

from skills.info_gathering.evals.function_learning.functions import SecretFunction
from skills.info_gathering.evals.twenty_questions.prompts import load_scratch_system_note
from util.bazel.runfiles import get_required_path

_FIRST_USER_MSG_RLOCATION = "_main/skills/info_gathering/evals/function_learning/first_user_message.txt"

_BASE_PREAMBLE = (
    "You are playing a function-learning game. There is a secret function "
    "f: [0, max_input] → [0, max_output]. Each turn, you query one input and submit a Python program "
    "that prints your best guess for f(0), f(1), ..., f(max_input) — one value per line, no input. "
    "Your goal is to minimize total Hamming loss (sum of bit disagreements across all possible inputs) "
    "summed over all turns."
)


def build_system_prompt(*, skill: str, has_scratch: bool, skill_files_path: Path | None) -> str:
    """Compose the system prompt for the function learning guesser.

    Args:
        skill: SKILL.md text to inline. Empty string for the off-arm of a
               mounted-skill rollout (the empty-skill tar's SKILL.md is blank).
        has_scratch: Include the scratch container exec tool note.
        skill_files_path: In-container path where the skill tar is bind-mounted
                          (e.g. ``/work/.skill``), or ``None`` when the rollout
                          doesn't mount the skill into a sandbox. Pass it
                          explicitly.
    """
    parts: list[str] = [_BASE_PREAMBLE]
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


def first_user_message(fn: SecretFunction, turn_limit: int, function_description: str, eval_timeout_s: int) -> str:
    template = get_required_path(_FIRST_USER_MSG_RLOCATION).read_text().strip()
    return template.format(
        max_input=fn.max_input,
        max_output=fn.max_output,
        n_inputs=fn.max_input + 1,
        turn_limit=turn_limit,
        function_description=function_description,
        eval_timeout_s=eval_timeout_s,
    )
