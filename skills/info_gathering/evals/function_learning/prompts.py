"""Prompt loading helpers for the function learning eval."""

from pathlib import Path

from skills.eval_infra.eval_prompt import compose_system_prompt
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
    """Compose the FL system prompt: preamble + skill block + (optional) scratch note."""
    return compose_system_prompt(
        preamble=_BASE_PREAMBLE,
        skill_md=skill,
        skill_files_path=skill_files_path,
        scratch_note=load_scratch_system_note() if has_scratch else None,
    )


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
