"""Run generated fixtures past the differential oracle and report what happened.

One campaign is a sequence of `(shape, value_seed)` cases. Each is built, run on both
engines and compared by `backend.assert_results_agree` — the same comparison
`assert_backends_agree` makes, reached through its parts only so that one engine refusing a
fixture the other ran can be told apart from the two disagreeing about an answer.

A campaign runs every case and then fails with one shrunk reproducer per distinct differing
channel, each written to the test's undeclared outputs so it can be replayed. A campaign that
finds none says how many cases of what kind it actually ran: the number is the whole claim.
"""

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from finance.augur.rust.differential.backend import assert_results_agree, run_jax, run_rust
from finance.augur.rust.differential.generator import Shape, build_fixture
from finance.augur.rust.differential.shrink import shrink_fixture
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

# What a campaign will spend turning a divergence into something readable. Almost every
# reduction changes the plan structure and so pays an XLA compile, so this is a handful of
# candidates rather than the whole search — and it has to leave the failing test enough of
# its Bazel timeout to print what it found.
SHRINK_SECONDS = 90.0


class Outcome(StrEnum):
    AGREED = "agreed"
    # The legacy JAX surface cannot express the fixture — one fixed sale price per scheduled
    # sale across rollouts, a ppb rate that does not survive the float boundary. Documented
    # narrowings of the comparison rather than answers that differ: counted, not failed on.
    UNREPRESENTABLE = "unrepresentable"
    DIVERGED = "diverged"


@dataclass(frozen=True)
class Case:
    shape: Shape
    value_seed: int

    def fixture(self) -> dict[str, Any]:
        return build_fixture(self.shape, self.value_seed)


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    # For a divergence, the channel or event frame that differs. Shrinking holds this fixed,
    # so a reduction that trades one disagreement for another is rejected.
    signature: str
    detail: str


def evaluate(fixture: dict[str, Any]) -> Verdict:
    try:
        jax_result = run_jax(fixture)
    except ValueError as error:
        return Verdict(Outcome.UNREPRESENTABLE, "", str(error))
    try:
        rust = run_rust(fixture)
    except ValueError as error:
        return Verdict(Outcome.DIVERGED, "rust refused the fixture", f"the JAX engine ran it: {error}")
    try:
        assert_results_agree(jax_result, rust)
    except AssertionError as error:
        return Verdict(Outcome.DIVERGED, str(error).splitlines()[0], str(error.__cause__))
    return Verdict(Outcome.AGREED, "", "")


@dataclass(frozen=True)
class Report:
    """What a campaign actually did.

    `compared` counts the cases both engines ran and the oracle compared — the only number
    that is evidence of anything. A case the JAX surface refused is counted apart, so a
    generator that drifts into producing them shrinks the claim instead of inflating it.
    """

    compared: int
    diverged: int
    unrepresentable: int
    shapes: int
    seconds: float

    def __str__(self) -> str:
        rate = (self.compared + self.unrepresentable) / self.seconds if self.seconds else 0.0
        return (
            f"{self.compared} cases compared over {self.shapes} shapes in {self.seconds:.1f}s "
            f"({rate:.1f}/s), {self.diverged} diverged, "
            f"{self.unrepresentable} unrepresentable on the JAX surface"
        )


def _report_divergence(case: Case, signature: str, budget_seconds: float) -> str:
    """Shrink the failing fixture, save it, and describe it by its own differing values."""

    def still_diverges(candidate: dict[str, Any]) -> bool:
        """The same channel still differs. The values themselves change as it shrinks."""

        reduced = evaluate(candidate)
        return reduced.outcome is Outcome.DIVERGED and reduced.signature == signature

    minimal, tried = shrink_fixture(case.fixture(), still_diverges=still_diverges, budget_seconds=budget_seconds)
    path = undeclared_outputs_dir() / f"divergence-{case.shape.name}-{case.value_seed}.json"
    path.write_text(json.dumps(minimal, indent=2, sort_keys=True))
    return (
        f"{signature}, first seen on shape {case.shape.name!r} value seed {case.value_seed}\n"
        # The reduced fixture's own numbers, not the original's: reading a shrunk reproducer
        # beside the values a bigger one produced is how you chase the wrong quantity.
        f"{evaluate(minimal).detail}\n"
        f"minimal reproducer after {tried} shrink candidates, written to {path}:\n"
        f"{json.dumps(minimal, indent=2, sort_keys=True)}"
    )


def run(cases: Iterable[Case]) -> Report:
    """Run every case, then fail with a shrunk reproducer per distinct divergence.

    Running on past the first is what makes a second campaign worth anything while a first
    finding is open: stopping there would say only what is already known.
    """

    started = time.monotonic()
    compared, diverged, unrepresentable, shapes = 0, 0, 0, set()
    first_case: dict[str, Case] = {}
    # One line per distinct refusal, not per case: a shape the surface will not take refuses
    # every seed the same way, and a thousand copies of it buries everything else in the log.
    refusals: set[tuple[str, str]] = set()
    for case in cases:
        shapes.add(case.shape.name)
        try:
            verdict = evaluate(case.fixture())
        except Exception as error:
            # Anything neither engine turned into a `ValueError` is still a finding, and a
            # finding nobody can reproduce is worth little — so it leaves carrying its case.
            error.add_note(f"raised on shape {case.shape.name!r} value seed {case.value_seed}")
            raise
        match verdict.outcome:
            case Outcome.DIVERGED:
                compared, diverged = compared + 1, diverged + 1
                first_case.setdefault(verdict.signature, case)
            case Outcome.UNREPRESENTABLE:
                unrepresentable += 1
                if (case.shape.name, verdict.detail) not in refusals:
                    refusals.add((case.shape.name, verdict.detail))
                    logger.warning(
                        "shape %s seed %d unrepresentable: %s", case.shape.name, case.value_seed, verdict.detail
                    )
            case Outcome.AGREED:
                compared += 1
    report = Report(
        compared=compared,
        diverged=diverged,
        unrepresentable=unrepresentable,
        shapes=len(shapes),
        seconds=time.monotonic() - started,
    )
    # Printed rather than logged: the count is the campaign's result, and `--test_output` is
    # where a reader looks for it.
    print(report)
    if first_case:
        budget = SHRINK_SECONDS / len(first_case)
        raise AssertionError(
            f"{len(first_case)} distinct divergences over {compared} compared cases\n\n"
            + "\n\n".join(_report_divergence(case, signature, budget) for signature, case in sorted(first_case.items()))
        )
    return report
