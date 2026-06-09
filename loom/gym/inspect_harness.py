"""Gym tasks as Inspect AI evals: agent contestants in a network-less Docker sandbox.

Each gym task becomes an inspect `Sample` whose `files` land the
as-of-truncated dossier under `/data` in the sandbox; `sandbox/compose.yaml`
sets `network_mode: none`, so the as-of discipline is physical, not
prompt-level. The react agent explores with bash/python tools and must call
`submit` with the bare answer JSON; the scorer applies the gym's proper
losses (full metric set in `Score.metadata`, headline metric as the value).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path

# Aliased to avoid colliding with the gym's own Task.
from inspect_ai import Task as InspectTask
from inspect_ai.agent import AgentSubmit, react
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import bash, python

from loom.gym import scoring
from loom.gym.baseline_llm import parse_answer
from loom.gym.dossier import series_dossier
from loom.gym.scoring import QUANTILE_LEVELS
from loom.gym.task import BinaryQuestion, ScalarQuestion, Task

logger = logging.getLogger(__name__)

COMPOSE_PATH = Path(__file__).parent / "sandbox" / "compose.yaml"

AGENT_PROMPT = (
    "You are a careful forecaster. Data files are available under /data (start with /data/README.txt); "
    "inspect them with the bash and python tools before answering. When confident, call submit with "
    "ONLY the JSON answer object, no prose."
)


def _answer_shape(task: Task) -> str:
    match task.question:
        case BinaryQuestion():
            return '{"p": <probability the question resolves YES, between 0 and 1>}'
        case ScalarQuestion(unit=unit):
            levels = ", ".join(f'"{level}"' for level in QUANTILE_LEVELS)
            return f'{{"quantiles": {{<level>: <value in {unit}>}}}} with exactly these levels: {levels}'


def sample_for_task(task: Task) -> Sample:
    instructions = (
        f"You are forecasting as of {task.as_of}. The /data files are truncated to what was knowable then; "
        f"use only them and knowledge of events on or before {task.as_of}.\n"
        f"Question: {task.question.text}\n"
        f"Resolution date: {task.resolution_date}\n"
        f"Submit ONLY this JSON shape: {_answer_shape(task)}"
    )
    return Sample(
        id=task.task_id,
        input=instructions,
        target=json.dumps(task.outcome.model_dump(mode="json")),
        files={f"/data/{name}": content for name, content in series_dossier(task.as_of).items()},
        metadata={"gym_task": task.model_dump(mode="json")},
    )


def headline_metric(metrics: dict[str, float], kind: str) -> str:
    if kind == "binary":
        return "log_loss"
    return "mean_pinball_log" if "mean_pinball_log" in metrics else "mean_pinball"


@scorer(metrics=[mean()])
def gym_proper_loss():
    async def score_fn(state: TaskState, target: Target) -> Score:
        gym_task = Task.model_validate(state.metadata["gym_task"])
        answer = parse_answer(gym_task, json.loads(state.output.completion))
        task_score = scoring.score(gym_task, answer)
        return Score(
            value=task_score.metrics[headline_metric(task_score.metrics, gym_task.question.kind)],
            answer=state.output.completion,
            metadata=task_score.metrics,
        )

    return score_fn


def agent_eval_task(tasks: Sequence[Task]) -> InspectTask:
    return InspectTask(
        dataset=MemoryDataset([sample_for_task(task) for task in tasks]),
        solver=react(
            prompt=AGENT_PROMPT, tools=[bash(timeout=120), python(timeout=120)], submit=AgentSubmit(answer_only=True)
        ),
        scorer=gym_proper_loss(),
        sandbox=("docker", str(COMPOSE_PATH)),
    )
