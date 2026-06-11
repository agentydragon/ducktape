"""Judge floor/ceiling validation runs.

These are NOT an eval of the RE agent — they're sanity checks on the
`rubric_judge` scorer. Each run pre-populates `/grade/recovered/` with a
known state and asks the judge to grade it:

  - validate_judge:empty       judge floor — empty `/grade/recovered/`,
                               expected score ~0.0.
  - validate_judge:reference   judge ceiling — reference *.go files in
                               `/grade/recovered/`, expected score >0.85.

If empty scores high or reference scores low, the judge prompt or the
rubric is broken. Fix that before trusting any agent score.

Run via:

    bb run //skills/reverse_engineer/evals/x:validate_empty
    bb run //skills/reverse_engineer/evals/x:validate_reference

Or, for explicit `--case`:

    bb run //skills/reverse_engineer/evals/x:validate_judge -- --case=empty
    bb run //skills/reverse_engineer/evals/x:validate_judge -- --case=reference
"""

from __future__ import annotations

import argparse

from skills.reverse_engineer.evals.x._runner import add_common_flags, run_eval
from skills.reverse_engineer.evals.x.task import validate_empty_work, validate_reference_work

_CASES = {"empty": validate_empty_work, "reference": validate_reference_work}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_flags(parser, default_model="anthropic/claude-sonnet-4-6")
    parser.add_argument("--case", choices=sorted(_CASES), required=True, help="Which validation case to run.")
    args = parser.parse_args()

    run_eval(args=args, log_subdir=f"validate_judge_logs/{args.case}", task_factory=_CASES[args.case])


if __name__ == "__main__":
    main()
