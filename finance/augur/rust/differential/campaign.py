"""Run generated cases past the differential oracle and report what happened.

One campaign is a sequence of `Trial`s — the generator coordinates of a case, a `Shape` and a
value seed. Each builds its `Case`, runs it on both engines and compares them with
`backend.assert_results_agree`: the same comparison `assert_backends_agree` makes, reached
through its parts only so that one engine refusing a case the other ran can be told apart
from the two disagreeing about an answer.

A campaign runs every trial and then fails with one shrunk reproducer per distinct differing
channel, each written to the test's undeclared outputs. A campaign that finds none says how
many cases of what kind it actually ran: the number is the whole claim.
"""

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from finance.augur.rust.case_fixture import fixture_for
from finance.augur.rust.differential.backend import assert_results_agree
from finance.augur.rust.differential.generator import Shape, build_case
from finance.augur.rust.differential.shrink import shrink_case
from finance.augur.rust.fixture_encoder import UnsupportedScenarioError
from finance.augur.rust.result import run_rust
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.jax_result import run_jax
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

# What a campaign will spend turning a divergence into something readable. Almost every
# reduction changes the plan structure and so pays an XLA compile, so this is a handful of
# candidates rather than the whole search — and it has to leave the failing test enough of
# its Bazel timeout to print what it found.
SHRINK_SECONDS = 90.0


class Outcome(StrEnum):
    AGREED = "agreed"
    # The Rust fixture cannot express the case — a rate finer than parts per billion, a lot
    # whose total basis is not whole quanta. A documented narrowing of the comparison rather
    # than an answer that differs: counted, not failed on.
    UNREPRESENTABLE = "unrepresentable"
    DIVERGED = "diverged"


@dataclass(frozen=True)
class Trial:
    """One case's generator coordinates: which compiled program, and which traced inputs.

    Not the case itself — `case.Case` is the authored scenario both engines run, and this is
    the pair that names one. Kept apart from the case because it is what a campaign reports
    and what regenerates the unshrunk case exactly.
    """

    shape: Shape
    value_seed: int

    def case(self) -> Case:
        return build_case(self.shape, self.value_seed)


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    # For a divergence, the channel or event frame that differs. Shrinking holds this fixed,
    # so a reduction that trades one disagreement for another is rejected.
    signature: str
    detail: str


def evaluate(case: Case) -> Verdict:
    """What the oracle says about one case.

    A JAX failure is not an outcome here. Both engines run the plan this case compiles, so
    JAX refusing it means the generator wrote a scenario the compiler will not take — a bug
    in the generator, which propagates rather than being counted as a narrowing.
    """

    jax_result = run_jax(case)
    try:
        rust = run_rust(case)
    except UnsupportedScenarioError as error:
        return Verdict(Outcome.UNREPRESENTABLE, "", str(error))
    except ValueError as error:
        return Verdict(Outcome.DIVERGED, "rust refused the case", f"the JAX engine ran it: {error}")
    try:
        assert_results_agree(jax_result, rust)
    except AssertionError as error:
        return Verdict(Outcome.DIVERGED, str(error).splitlines()[0], str(error.__cause__))
    return Verdict(Outcome.AGREED, "", "")


@dataclass(frozen=True)
class Report:
    """What a campaign actually did.

    `compared` counts the cases both engines ran and the oracle compared — the only number
    that is evidence of anything. A case the Rust fixture cannot express is counted apart, so
    a generator that drifts into producing them shrinks the claim instead of inflating it.
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
            f"{self.unrepresentable} unrepresentable in the Rust fixture"
        )


def _report_divergence(trial: Trial, signature: str, budget_seconds: float) -> str:
    """Shrink the failing case, save it, and describe it by its own differing values."""

    def still_diverges(candidate: Case) -> bool:
        """The same channel still differs. The values themselves change as it shrinks."""

        reduced = evaluate(candidate)
        return reduced.outcome is Outcome.DIVERGED and reduced.signature == signature

    minimal, tried = shrink_case(trial.case(), still_diverges=still_diverges, budget_seconds=budget_seconds)
    stem = f"divergence-{trial.shape.name}-{trial.value_seed}"
    # Two artifacts because they answer different questions: the scenario is what a
    # `known_divergence_test` entry is re-authored from, and the fixture is the integer
    # document Rust ran — the encoding of the very plan JAX ran, so it states every number
    # the comparison was made on, tax tables included.
    (undeclared_outputs_dir() / f"{stem}.scenario.json").write_text(minimal.scenario.model_dump_json(indent=2))
    fixture = json.dumps(fixture_for(minimal), indent=2, sort_keys=True)
    fixture_path = undeclared_outputs_dir() / f"{stem}.fixture.json"
    fixture_path.write_text(fixture)
    return (
        f"{signature}, first seen on shape {trial.shape.name!r} value seed {trial.value_seed}\n"
        # The reduced case's own numbers, not the original's: reading a shrunk reproducer
        # beside the values a bigger one produced is how you chase the wrong quantity.
        f"{evaluate(minimal).detail}\n"
        f"minimal reproducer after {tried} shrink candidates, written to {fixture_path}:\n"
        f"{fixture}"
    )


def run(trials: Iterable[Trial]) -> Report:
    """Run every trial, then fail with a shrunk reproducer per distinct divergence.

    Running on past the first is what makes a second campaign worth anything while a first
    finding is open: stopping there would say only what is already known.
    """

    started = time.monotonic()
    compared, diverged, unrepresentable, shapes = 0, 0, 0, set()
    first_trial: dict[str, Trial] = {}
    # One line per distinct refusal, not per case: a shape the fixture will not take refuses
    # every seed the same way, and a thousand copies of it buries everything else in the log.
    refusals: set[tuple[str, str]] = set()
    for trial in trials:
        shapes.add(trial.shape.name)
        try:
            verdict = evaluate(trial.case())
        except Exception as error:
            # Anything neither engine turned into a `ValueError` is still a finding, and a
            # finding nobody can reproduce is worth little — so it leaves carrying its trial.
            error.add_note(f"raised on shape {trial.shape.name!r} value seed {trial.value_seed}")
            raise
        match verdict.outcome:
            case Outcome.DIVERGED:
                compared, diverged = compared + 1, diverged + 1
                first_trial.setdefault(verdict.signature, trial)
            case Outcome.UNREPRESENTABLE:
                unrepresentable += 1
                if (trial.shape.name, verdict.detail) not in refusals:
                    refusals.add((trial.shape.name, verdict.detail))
                    logger.warning(
                        "shape %s seed %d unrepresentable: %s", trial.shape.name, trial.value_seed, verdict.detail
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
    if first_trial:
        budget = SHRINK_SECONDS / len(first_trial)
        raise AssertionError(
            f"{len(first_trial)} distinct divergences over {compared} compared cases\n\n"
            + "\n\n".join(
                _report_divergence(trial, signature, budget) for signature, trial in sorted(first_trial.items())
            )
        )
    return report
