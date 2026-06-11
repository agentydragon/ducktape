"""Program evaluation for the function learning eval.

Runs the model's program in a Docker container and compares its stdout lines
against the secret function's outputs. The program takes no input and prints
2^n lines — one per input from 0 to 2^n - 1.
"""

import asyncio
import logging
from dataclasses import dataclass

import aiodocker

from skills.info_gathering.evals.function_learning.functions import SecretFunction
from skills.info_gathering.evals.function_learning.result_types import ProgramError, ProgramScore

logger = logging.getLogger(__name__)

EVAL_TIMEOUT_S = 30
_MAX_REPORTED_ERRORS = 5


def _hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _max_output_bytes(secret_fn: SecretFunction) -> int:
    """Upper bound on expected stdout size, with 2x safety margin."""
    digits = len(str(secret_fn.max_output))
    n_lines = secret_fn.max_input + 1
    return (digits + 1) * n_lines * 2


@dataclass
class ScoringResult:
    score: ProgramScore
    total_eval_s: float


def _max_loss(secret_fn: SecretFunction) -> int:
    return (secret_fn.max_input + 1) * secret_fn.m


def _fail_result(error_msg: str, secret_fn: SecretFunction, total_eval_s: float) -> ScoringResult:
    return ScoringResult(
        score=ProgramScore(
            hamming_loss=_max_loss(secret_fn),
            missing_lines=secret_fn.max_input + 1,
            examples=[ProgramError(line=0, error=error_msg)],
        ),
        total_eval_s=total_eval_s,
    )


async def _docker_exec(
    container: aiodocker.docker.DockerContainer, cmd: list[str], timeout_s: int, max_bytes: int
) -> tuple[str, bool]:
    """Run a command in a container and return (stdout, timed_out)."""
    exec_obj = await container.exec(cmd, stdout=True, stderr=True, stdin=False, tty=False)
    stream = exec_obj.start()
    chunks: list[bytes] = []
    total = 0
    timed_out = False
    try:
        async with asyncio.timeout(timeout_s):
            while msg := await stream.read_out():
                chunks.append(msg.data)
                total += len(msg.data)
                if total >= max_bytes:
                    break
    except TimeoutError:
        timed_out = True
    return b"".join(chunks).decode("utf-8", errors="replace"), timed_out


def _score_lines(lines: list[str], secret_fn: SecretFunction) -> ProgramScore:
    """Score output lines against the secret function."""
    n_inputs = secret_fn.max_input + 1
    m = secret_fn.m
    max_output = secret_fn.max_output

    hamming_loss = 0
    parse_errors = 0
    out_of_range = 0
    missing_lines = max(0, n_inputs - len(lines))
    examples: list[ProgramError] = []

    for i in range(n_inputs):
        expected = secret_fn.evaluate(i)
        if i >= len(lines):
            hamming_loss += m
            continue
        raw = lines[i].strip()
        try:
            val = int(raw)
        except ValueError:
            parse_errors += 1
            hamming_loss += m
            if len(examples) < _MAX_REPORTED_ERRORS:
                examples.append(ProgramError(line=i, error=f"Not an integer: {raw!r}"))
            continue
        if val < 0 or val > max_output:
            out_of_range += 1
            hamming_loss += m
            if len(examples) < _MAX_REPORTED_ERRORS:
                examples.append(ProgramError(line=i, error=f"Out of range [0, {max_output}]: {val}"))
            continue
        hamming_loss += _hamming_distance(expected, val)

    return ProgramScore(
        hamming_loss=hamming_loss,
        parse_errors=parse_errors,
        out_of_range=out_of_range,
        missing_lines=missing_lines,
        examples=examples,
    )


async def evaluate_program(
    container: aiodocker.docker.DockerContainer, program: str, secret_fn: SecretFunction
) -> ScoringResult:
    """Evaluate program against all inputs by comparing stdout lines."""
    max_bytes = _max_output_bytes(secret_fn)
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    raw, timed_out = await _docker_exec(container, ["python3", "-c", program], EVAL_TIMEOUT_S, max_bytes)
    total_eval_s = loop.time() - t0

    if timed_out and not raw.strip():
        return _fail_result("Program timed out with no output", secret_fn, total_eval_s)

    lines = raw.splitlines()
    score = _score_lines(lines, secret_fn)
    return ScoringResult(score=score, total_eval_s=total_eval_s)
