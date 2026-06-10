"""Gym tasks as Inspect AI evals: agent contestants in a date-clamped sandbox.

Each gym task becomes an inspect `Sample` whose `files` land the
as-of-truncated dossier under `/data`. The sandbox compose is generated per
`as_of`: the agent container's only network route is the wayback proxy
sidecar (`loom/wayback_proxy`), which answers every URL with the newest
Internet Archive capture at-or-before the task's `as_of` — the as-of
discipline stays physical (no direct egress) while the agent can research the
pre-cutoff web. The react agent explores with bash/python tools and must call
`submit` with the bare answer JSON; the scorer applies the gym's proper
losses (full metric set in `Score.metadata`, headline metric as the value).

Runs need the proxy image on the local Docker daemon
(`bazelisk run //loom/wayback_proxy:load`); for real evals point
`wayback_upstream` at the shared cluster pull-through cache
(`cluster/k8s/wayback-cache/`, e.g. via kubectl port-forward) so IA is never
hammered directly.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

# Aliased to avoid colliding with the gym's own Task.
from inspect_ai import Task as InspectTask
from inspect_ai.agent import AgentSubmit, react
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import bash, python
from inspect_ai.util import sandbox

from loom.gym import scoring
from loom.gym.baseline_llm import answer_instruction, parse_answer
from loom.gym.dossier import series_dossier
from loom.gym.monthly_series import MonthlySeries
from loom.gym.task import Task

logger = logging.getLogger(__name__)

WAYBACK_PROXY_IMAGE_TAG = "wayback-proxy:latest"
DEFAULT_WAYBACK_UPSTREAM = "https://web.archive.org"

# Generated per as_of: WAYBACK_AS_OF is a baked literal, not compose env
# interpolation, so the clamp is pinned per sandbox and can never be
# influenced from inside the agent container.
_COMPOSE_TEMPLATE = """\
services:
  default:
    image: python:3.13-slim
    x-local: true
    init: true
    command: tail -f /dev/null
    cpus: 1.0
    mem_limit: 1gb
    # The internal network's only other member is the proxy: every web
    # request goes through the date clamp; nothing else is routable.
    networks: [sandbox]
    environment:
      http_proxy: http://proxy:8080
      HTTP_PROXY: http://proxy:8080
    depends_on:
      proxy:
        condition: service_healthy

  proxy:
    # x-local: the proxy image is bazel-built and docker-loaded, never in a
    # registry — without this Inspect attempts a doomed `compose pull`.
    image: {image}
    x-local: true
    init: true
    networks: [sandbox, egress]
    environment:
      WAYBACK_AS_OF: "{as_of}"
      WAYBACK_UPSTREAM: "{upstream}"
      WAYBACK_MANIFEST_PATH: "{manifest_path}"
    extra_hosts:
      # Lets upstream point at a host port: a kubectl port-forward of the
      # cluster wayback-cache, or a test's in-process fake IA.
      - host.docker.internal:host-gateway
    healthcheck:
      test:
        - CMD
        - python3
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)
      interval: 2s
      timeout: 5s
      retries: 15
      start_period: 10s

networks:
  sandbox:
    internal: true
  egress: {{}}
"""

AGENT_PROMPT = (
    "You are a careful forecaster. Data files are available under /data (start with /data/README.txt); "
    "inspect them with the bash and python tools. If /data/evidence.jsonl exists, it lists dated leads "
    "(url, date, title) published on or before your information cutoff. You can fetch those urls — and "
    "browse the web as it existed at the cutoff generally — through a preconfigured HTTP proxy: use "
    "plain http:// URLs (urllib and requests honor http_proxy automatically; https and the live web "
    "are unreachable). When confident, call submit with ONLY the JSON answer object, no prose."
)

# Container path the proxy sidecar writes its served-evidence manifest to;
# the scorer reads it back per sample (W3 of the wayback proxy plan).
MANIFEST_PATH = "/tmp/wayback-manifest.jsonl"


def write_sandbox_compose(directory: Path, as_of: date, upstream: str) -> Path:
    """The sandbox compose for one as_of: agent's only route is the clamped proxy."""
    path = directory / f"sandbox-{as_of}.yaml"
    path.write_text(
        _COMPOSE_TEMPLATE.format(
            image=WAYBACK_PROXY_IMAGE_TAG, as_of=as_of, upstream=upstream, manifest_path=MANIFEST_PATH
        )
    )
    return path


def _evidence_jsonl(task: Task) -> str:
    """Evidence leads as a /data file, not prompt text: read only if the agent
    chooses (tokens pay per use), uniform with the rest of the dossier, and
    leads stay data to evaluate rather than harness-asserted facts."""
    return "".join(
        json.dumps({"url": item.url, "date": str(item.date), "title": item.title}) + "\n" for item in task.evidence
    )


def sample_for_task(task: Task, dossier: dict[str, str], compose_path: Path) -> Sample:
    instructions = (
        f"You are forecasting as of {task.as_of}. The /data files are truncated to what was knowable then, "
        f"and the web proxy serves pages as archived on or before {task.as_of}; use only those sources and "
        f"knowledge of events on or before {task.as_of}.\n"
        f"Question: {task.question.text}\n"
        f"Resolution date: {task.resolution_date}\n"
        f"Submit ONLY a JSON object: {answer_instruction(task.question)}."
    )
    files = {f"/data/{name}": content for name, content in dossier.items()}
    if task.evidence:
        files["/data/evidence.jsonl"] = _evidence_jsonl(task)
    return Sample(
        id=task.task_id,
        input=instructions,
        target=json.dumps(task.outcome.model_dump(mode="json")),
        files=files,
        metadata={"gym_task": task.model_dump(mode="json")},
        sandbox=("docker", str(compose_path)),
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
        try:
            manifest_text = await sandbox("proxy").read_file(MANIFEST_PATH)
        except FileNotFoundError:
            # The proxy creates the manifest lazily on its first served
            # response; absence just means this sample fetched nothing.
            manifest_text = ""
        served = [json.loads(line) for line in manifest_text.splitlines() if line]
        return Score(
            value=task_score.metrics[headline_metric(task_score.metrics, gym_task.question.kind)],
            answer=state.output.completion,
            metadata={**task_score.metrics, "served_evidence": served},
        )

    return score_fn


def agent_eval_task(
    tasks: Sequence[Task],
    series: Sequence[MonthlySeries],
    *,
    wayback_upstream: str = DEFAULT_WAYBACK_UPSTREAM,
    compose_dir: Path | None = None,
) -> InspectTask:
    """Inspect task over gym tasks; one sandbox compose is generated per distinct as_of."""
    if compose_dir is None:
        compose_dir = Path(tempfile.mkdtemp(prefix="gym-sandbox-"))
    as_ofs = {task.as_of for task in tasks}
    dossiers = {as_of: series_dossier(series, as_of) for as_of in as_ofs}
    composes = {as_of: write_sandbox_compose(compose_dir, as_of, wayback_upstream) for as_of in as_ofs}
    return InspectTask(
        dataset=MemoryDataset([sample_for_task(task, dossiers[task.as_of], composes[task.as_of]) for task in tasks]),
        solver=react(
            prompt=AGENT_PROMPT, tools=[bash(timeout=120), python(timeout=120)], submit=AgentSubmit(answer_only=True)
        ),
        scorer=gym_proper_loss(),
    )
