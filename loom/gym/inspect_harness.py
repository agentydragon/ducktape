"""Gym tasks as Inspect AI evals: agent contestants in a date-clamped sandbox.

Each gym task becomes an inspect `Sample` whose `files` land the
as-of-truncated dossier under `/data`. The sandbox compose is generated per
`as_of`: the agent container's only network route is the wayback proxy
sidecar (`loom/wayback_proxy`), which answers every URL with the newest
Internet Archive capture at-or-before the task's `as_of` — the as-of
discipline stays physical (no direct egress) while the agent can research the
pre-cutoff web. The react agent explores with a single bash tool (python3 +
a data toolkit are in the sandbox image, run via the shell) and must call
`submit` with the bare answer JSON; the scorer applies the gym's proper
losses (full metric set in `Score.metadata`, headline metric as the value).

Runs need two images on the local Docker daemon: the proxy
(`bazelisk run //loom/wayback_proxy:load`) and the agent sandbox
(`docker build -t loom-gym-sandbox:latest loom/gym/sandbox/`). For real evals
point `wayback_upstream` at the shared cluster pull-through cache so IA is
never hammered directly — from out of cluster use its authed gateway route
(`https://wayback-cache.allegedly.works` + `wayback_upstream_auth="Bearer
<token>"` from the `wayback-cache-token` secret); in-cluster use the
unauthed `http://wayback-cache.wayback-cache.svc:8080`.
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
from inspect_ai.tool import bash
from inspect_ai.util import sandbox

from loom.gym import scoring
from loom.gym.baseline_llm import answer_instruction, parse_answer
from loom.gym.dossier import series_dossier
from loom.gym.monthly_series import MonthlySeries
from loom.gym.task import Task

logger = logging.getLogger(__name__)

WAYBACK_PROXY_IMAGE_TAG = "wayback-proxy:latest"
# Built from loom/gym/sandbox/Dockerfile: python:3.13-slim + pandas, numpy,
# scipy, statsmodels, python-dateutil, requests, bash, curl.
SANDBOX_IMAGE_TAG = "loom-gym-sandbox:latest"
DEFAULT_WAYBACK_UPSTREAM = "https://web.archive.org"

# Standard host CA bundle. In a TLS-inspecting egress environment (e.g. Claude
# Code web) it carries the inspection CA, so mounting it into the proxy lets the
# proxy's upstream HTTPS to the cache/IA validate. Elsewhere it's just the host's
# normal public trust — mounting it is a harmless no-op. The agent container
# never gets it (its only route is the proxy, whose own MITM CA it already
# trusts); only the proxy's outbound hop needs it.
HOST_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
_PROXY_EGRESS_CA = "/etc/ssl/proxy-egress-ca.crt"

# Generated per as_of: WAYBACK_AS_OF is a baked literal, not compose env
# interpolation, so the clamp is pinned per sandbox and can never be
# influenced from inside the agent container.
_COMPOSE_TEMPLATE = """\
services:
  default:
    image: {agent_image}
    x-local: true
    init: true
    command: tail -f /dev/null
    cpus: 1.0
    mem_limit: 2gb
    # The internal network's only other member is the proxy: every web
    # request goes through the date clamp; nothing else is routable.
    networks: [sandbox]
    environment:
      http_proxy: http://proxy:8080
      HTTP_PROXY: http://proxy:8080
      https_proxy: http://proxy:8080
      HTTPS_PROXY: http://proxy:8080
      # The proxy MITMs TLS with its own CA (written to the shared volume at
      # startup); trust it through the standard env-var contract so https://
      # fetches validate without rewriting to http://.
      SSL_CERT_FILE: /wayback-ca/mitmproxy-ca-cert.pem
      REQUESTS_CA_BUNDLE: /wayback-ca/mitmproxy-ca-cert.pem
      CURL_CA_BUNDLE: /wayback-ca/mitmproxy-ca-cert.pem
      NODE_EXTRA_CA_CERTS: /wayback-ca/mitmproxy-ca-cert.pem
    volumes:
      - wayback-ca:/wayback-ca:ro
    depends_on:
      proxy:
        condition: service_healthy

  proxy:
    # x-local: the proxy image is bazel-built and docker-loaded, never in a
    # registry — without this Inspect attempts a doomed `compose pull`.
    image: {image}
    x-local: true
    init: true
    # Run as root only to populate the fresh (root-owned) wayback-ca volume
    # with the generated CA; the agent mounts it read-only.
    user: "0:0"
    networks: [sandbox, egress]
    environment:
      WAYBACK_AS_OF: "{as_of}"
      WAYBACK_UPSTREAM: "{upstream}"
      WAYBACK_UPSTREAM_AUTH: "{upstream_auth}"
      WAYBACK_MANIFEST_PATH: "{manifest_path}"
      WAYBACK_CONFDIR: /wayback-ca
{proxy_egress_ca_env}    volumes:
      - wayback-ca:/wayback-ca
{proxy_egress_ca_volume}    extra_hosts:
      # Lets upstream point at a host port: a kubectl port-forward of the
      # cluster wayback-cache, or a test's in-process fake IA.
      - host.docker.internal:host-gateway
    healthcheck:
      # Probe through the proxy (mitmproxy has no origin-form endpoint): the
      # addon answers the reserved wayback-proxy.local host directly.
      test:
        - CMD
        - python3
        - -c
        # Single-quoted YAML scalar: the inline dict's ": " would otherwise make
        # YAML parse this block-sequence item as a mapping (yaml: did not find expected key).
        - 'import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({{"http": "http://127.0.0.1:8080"}})).open("http://wayback-proxy.local/healthz", timeout=3)'
      interval: 2s
      timeout: 5s
      retries: 15
      start_period: 10s

networks:
  sandbox:
    internal: true
  egress: {{}}

volumes:
  wayback-ca: {{}}
"""

AGENT_PROMPT = (
    "You are a careful forecaster. You have one tool: bash. Data files are under /data (start with "
    "/data/README.txt). Each bash call runs in a FRESH shell — variables and working state do NOT "
    "persist between calls, so make every command self-contained. python3 is available; the image "
    "(loom/gym/sandbox/Dockerfile) has pandas, numpy, scipy, statsmodels, python-dateutil, requests, "
    "and curl preinstalled, so run analysis with e.g. `python3 - <<'PY' ... PY` or `python3 -c '...'`. "
    "If /data/sources.txt exists, it lists URLs that may contain relevant information — possible "
    "starting points for your research, which you can follow and branch out from. You can browse the "
    "web as it existed at your information cutoff through a preconfigured proxy: ordinary http:// and "
    "https:// URLs both work (curl, urllib, and requests honor the proxy and its CA automatically). "
    "When confident, call submit with ONLY the JSON answer object, no prose."
)

# Container path the proxy sidecar writes its served-evidence manifest to;
# the scorer reads it back per sample (W3 of the wayback proxy plan).
MANIFEST_PATH = "/tmp/wayback-manifest.jsonl"


def write_sandbox_compose(
    directory: Path, as_of: date, upstream: str, agent_image: str = SANDBOX_IMAGE_TAG, upstream_auth: str = ""
) -> Path:
    """The sandbox compose for one as_of: agent's only route is the clamped proxy."""
    path = directory / f"sandbox-{as_of}.yaml"
    # Mount the host CA bundle into the proxy when present so its upstream HTTPS
    # validates behind a TLS-inspecting egress proxy (the agent never sees it).
    if HOST_CA_BUNDLE.is_file():
        proxy_egress_ca_env = f"      SSL_CERT_FILE: {_PROXY_EGRESS_CA}\n      REQUESTS_CA_BUNDLE: {_PROXY_EGRESS_CA}\n"
        proxy_egress_ca_volume = f"      - {HOST_CA_BUNDLE}:{_PROXY_EGRESS_CA}:ro\n"
    else:
        proxy_egress_ca_env = proxy_egress_ca_volume = ""
    path.write_text(
        _COMPOSE_TEMPLATE.format(
            agent_image=agent_image,
            image=WAYBACK_PROXY_IMAGE_TAG,
            as_of=as_of,
            upstream=upstream,
            upstream_auth=upstream_auth,
            manifest_path=MANIFEST_PATH,
            proxy_egress_ca_env=proxy_egress_ca_env,
            proxy_egress_ca_volume=proxy_egress_ca_volume,
        )
    )
    return path


_SOURCES_HEADER = (
    "URLs that may contain information relevant to this question, archived at or before your "
    "information cutoff. They are possible starting points only — fetch them (http:// or https://) "
    "through the proxy and branch out as you see fit.\n"
)


def _sources_txt(task: Task) -> str:
    """Evidence leads as a plaintext URL list in /data — not prompt text, and
    URLs only (no titles): the agent reads it if it chooses (tokens pay per
    use), titles never leak a curator's framing, and the URLs stay leads to
    investigate rather than harness-asserted facts."""
    return _SOURCES_HEADER + "".join(f"{item.url}\n" for item in task.evidence)


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
        files["/data/sources.txt"] = _sources_txt(task)
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


async def _served_evidence() -> list[dict[str, object]]:
    try:
        manifest_text = await sandbox("proxy").read_file(MANIFEST_PATH)
    except FileNotFoundError:
        # The proxy creates the manifest lazily on its first served response;
        # absence just means this sample fetched nothing.
        return []
    return [json.loads(line) for line in manifest_text.splitlines() if line]


@scorer(metrics=[mean()])
def gym_proper_loss():
    async def score_fn(state: TaskState, target: Target) -> Score:
        gym_task = Task.model_validate(state.metadata["gym_task"])
        served = await _served_evidence()
        # A contestant that emits no parseable answer (ran out of turns, wrote
        # prose) is a non-submission, not a crash: record it as NaN with the
        # reason so the run can report a submission rate separately from loss.
        try:
            answer = parse_answer(gym_task, json.loads(state.output.completion))
        except (json.JSONDecodeError, ValueError, KeyError) as error:
            return Score(
                value=float("nan"),
                answer=state.output.completion,
                metadata={"submission_error": f"{type(error).__name__}: {error}", "served_evidence": served},
            )
        task_score = scoring.score(gym_task, answer)
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
    wayback_upstream_auth: str = "",
    agent_image: str = SANDBOX_IMAGE_TAG,
    compose_dir: Path | None = None,
) -> InspectTask:
    """Inspect task over gym tasks; one sandbox compose is generated per distinct as_of."""
    if compose_dir is None:
        compose_dir = Path(tempfile.mkdtemp(prefix="gym-sandbox-"))
    as_ofs = {task.as_of for task in tasks}
    dossiers = {as_of: series_dossier(series, as_of) for as_of in as_ofs}
    composes = {
        as_of: write_sandbox_compose(compose_dir, as_of, wayback_upstream, agent_image, wayback_upstream_auth)
        for as_of in as_ofs
    }
    return InspectTask(
        dataset=MemoryDataset([sample_for_task(task, dossiers[task.as_of], composes[task.as_of]) for task in tasks]),
        solver=react(prompt=AGENT_PROMPT, tools=[bash(timeout=180)], submit=AgentSubmit(answer_only=True)),
        scorer=gym_proper_loss(),
    )
