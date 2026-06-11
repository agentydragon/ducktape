"""Bazel-runnable entry point for the Inspect-AI reverse-engineering eval.

Run via:

    bb run //skills/reverse_engineer/evals/x:run -- \
        --model anthropic/claude-haiku-4-5-20251001

Both Anthropic and OpenAI models are supported through Inspect's standard
provider strings (`anthropic/<model>`, `openai/<model>`,
`openai-api/<endpoint>/<model>`, etc.).

Prompt caching: auto-enabled by Inspect's Anthropic provider for any
caching-eligible Claude model. No flag to set.

Strict tool use: auto-enabled for OpenAI / openai-api provider tools
(Inspect sets `strict: true` on every custom tool's input schema). Native
Anthropic strict tool mode is not yet wired in Inspect, so this lever is
OpenAI-only.

Judge sanity-check entrypoints (floor / ceiling) live in `validate_judge.py`
— they are not an eval of the agent, just a check on the rubric scorer.
"""

from __future__ import annotations

import argparse
import os

from skills.reverse_engineer.evals.x._runner import add_common_flags, run_eval
from skills.reverse_engineer.evals.x.task import reverse_engineer_go_crypto


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_flags(parser, default_model="anthropic/claude-haiku-4-5-20251001")
    parser.add_argument("--message-limit", type=int, default=1000)
    parser.add_argument(
        "--judge-model", default=None, help="Rubric judge model (default: anthropic/claude-sonnet-4-6)."
    )
    parser.add_argument(
        "--time-limit",
        type=int,
        default=43200,
        help="Per-sample wall-clock budget in seconds (default: 12h). "
        "Inspect splits this: agent gets the full budget, scorer gets time_limit/2.",
    )
    args = parser.parse_args()

    def _stamp_snapshot_dir(log_dir: object) -> None:
        # Tell the snapshot solver where to drop the agent's `/work/` contents.
        # task.py reads `$RE_EVAL_SNAPSHOT_DIR` and writes `work_<sample_id>/`
        # next to the .eval log so the recovered source lives alongside the
        # rollout that produced it.
        os.environ["RE_EVAL_SNAPSHOT_DIR"] = str(log_dir)

    judge_model = args.judge_model or "anthropic/claude-sonnet-4-6"

    run_eval(
        args=args,
        log_subdir="eval_logs",
        task_factory=lambda: reverse_engineer_go_crypto(
            message_limit=args.message_limit, time_limit=args.time_limit, judge_model=judge_model
        ),
        pre_eval=_stamp_snapshot_dir,
    )


if __name__ == "__main__":
    main()
