"""Inspect AI port of the reverse-engineering eval.

Three @task variants live here:

- `reverse_engineer_go_crypto` — the real eval. React agent recovers Go
  source from the garbled binary, then `rubric_judge` grades it.
- `validate_empty_work` — pre-populates `/work/` with nothing; expected
  judge floor (~0.0). Sanity check that the judge isn't giving free
  credit.
- `validate_reference_work` — pre-populates `/work/` with the reference
  Go source files. Expected judge ceiling (>0.85). Sanity check that
  the judge actually rewards correct recoveries.

The judge follows the SWE-bench / inspect_evals pattern: TypedDict →
JSON schema → ToolDef → forced `tool_choice` → TypeAdapter validation.
That gives us provider-agnostic structured output (no
`response_schema` needed) and works on Anthropic + OpenAI alike.

Prompt caching: inspect_ai's Anthropic provider auto-marks the system
prompt and tool definitions with `cache_control: ephemeral`; nothing to
configure on this side. For OpenAI-compatible models, `strict_tools=True`
is the default in the openai-api provider, so the grader's `submit_grade`
tool is grammar-constrained.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from textwrap import dedent
from typing import Literal, NotRequired, TypedDict

import yaml
from inspect_ai import Task, task
from inspect_ai.agent import AgentPrompt, AgentState, AgentSubmit, as_solver, react
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ChatMessageUser
from inspect_ai.scorer import Score, Scorer, Target, accuracy, scorer, stderr
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import Tool, ToolDef, bash, tool
from inspect_ai.util import SandboxEnvironmentSpec, sandbox
from pydantic import TypeAdapter, ValidationError

from skills.eval_infra.skill_staging import stage_skill
from skills.reverse_engineer.reverse_engineer_skill_spec import SPEC as RE_SKILL_SPEC
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

# Runfiles paths for the specimen artifacts produced by the Bazel macros
# (`garble_binary`); the skill archive comes from the generated
# `RE_SKILL_SPEC` (`skill_package`).
_TARGET_BINARY_RLOCATION = "_main/skills/reverse_engineer/evals/tasks/go_crypto_server/go_crypto_server_garbled.bin"
_COMPOSE_RLOCATION = "_main/skills/reverse_engineer/evals/x/compose.yaml"
_SPECIMEN_DIR_RLOCATION = "_main/skills/reverse_engineer/evals/tasks/go_crypto_server"
_RUBRIC_RLOCATION = f"{_SPECIMEN_DIR_RLOCATION}/RUBRIC.yaml"
_SPEC_RLOCATION = f"{_SPECIMEN_DIR_RLOCATION}/SPEC.md"

# Env var the runner stamps with the host directory we want the agent's
# `/work/` snapshot to land in. Set in `run.py` before `inspect_eval()`
# so the snapshot solver can find it; we keep it on the task side instead
# of passing through Sample.metadata so the snapshot path doesn't
# accidentally get serialized into the .eval log header.
_SNAPSHOT_DIR_ENV = "RE_EVAL_SNAPSHOT_DIR"

# In-container layout (matches the existing AF-driven eval so the agent
# experiences the same shape — task framing in the first user message,
# binary at /input/target, scratch workspace at /work, skill tree at
# /skill).
_INPUT_PATH = "/input"
_WORK_PATH = "/work"
_SKILL_PATH = "/skill"


# Same task framing as `re_rollout._FIRST_USER_MESSAGE` — the agent is
# free-form and only the first-user prompt mentions paths. Wall-clock and
# step bounds are enforced by the harness and not disclosed.
_FIRST_USER_MESSAGE = (
    f"Reverse-engineer the stripped Go binary at {_INPUT_PATH}/target into a "
    f"complete, human-readable Go source tree under {_WORK_PATH}/ (your working "
    "directory). The recovered source should compile and be behaviorally "
    "equivalent to the binary — same protocol, same endpoints, same "
    "crypto/encoding/MAC/storage semantics — not stubs, placeholders, or TODOs. "
    "Pick reasonable, idiomatic Go names where the binary's were obfuscated. "
    "When done, call `submit` with a one-paragraph summary noting which parts "
    "you are confident vs unsure about."
)


_SYSTEM_PROMPT_TEMPLATE = """\
Follow this skill throughout the task.

<skill>
{skill_md}
</skill>

The full skill (SKILL.md and any referenced example files) is available in the
container at {skill_path}/.

---

You have shell access via the `bash` tool. Stdout and stderr are returned
together. The container is `python:3.13-slim` (Debian-based) with internet
access; install whatever you need (`apt-get install -y ...`,
`pip install ...`, `curl ...`).
"""


def _stage_skill() -> Path:
    """Extract the reverse_engineer skill to a temp dir and return the
    directory holding SKILL.md (i.e. `<tmp>/<package_name>`)."""
    # Persistent temp dir — must outlive the task() call because Sample.files
    # paths are resolved at sample setup time (after task() returns). The
    # process exit cleans it up.
    dest = Path(tempfile.mkdtemp(prefix="re_eval_skill_"))
    return stage_skill(RE_SKILL_SPEC, dest).files_path


def _stage_target_binary() -> Path:
    """Copy the garbled binary to a stable temp path so Sample.files can
    refer to it. (Inspect resolves Sample.files paths against its own CWD,
    not against runfiles, so we make the path absolute here.)"""
    src = get_required_path(_TARGET_BINARY_RLOCATION)
    dest_dir = Path(tempfile.mkdtemp(prefix="re_eval_input_"))
    dest = dest_dir / "target"
    shutil.copy(src, dest)
    return dest


@tool
def submit_summary() -> Tool:
    """Submit tool that captures the agent's free-form recovery summary.

    Replaces the react default's `answer: str` parameter with `summary: str`
    so the prompt the model sees lines up with the first-user-message
    instruction ("call `submit` with a one-paragraph summary..."). The
    return value is the summary itself, which is what react() forwards as
    the eval answer for the scorer.
    """

    async def execute(summary: str) -> str:
        """Mark the rollout complete with a final summary.

        Args:
            summary: One-paragraph summary noting which parts you are
                confident vs unsure about.
        """
        return summary

    return execute


# --- Rubric LLM judge --------------------------------------------------------
#
# Two-sandbox design. The agent's docker sandbox is torn down by Inspect at
# end of sample; we never expose the reference source to it. The grader gets
# its OWN container (started by the scorer via plain `docker run`, no
# Inspect sandbox plumbing) with read-only bind mounts:
#
#   /grade/recovered/         — agent's /work/ snapshot (host-side dir)
#   /grade/reference/         — specimen .go files (ground truth)
#   /grade/spec/SPEC.md
#   /grade/rubric/RUBRIC.yaml
#
# Inside that container the scorer drives a `react()` loop with `bash` plus
# a forced `submit_grade` tool. The judge runs find/cat/diff against the
# mounts on demand instead of getting prompt-stuffed with 800 KB of source.
# `submit_grade`'s arguments are schema-validated via TypedDict + TypeAdapter
# (same idiom inspect_evals/utils/scorers.py uses).


# Per-rubric-item grade. `score` is integer 0/1/2 (none / partial / full).
# `justification` is required — without it the grader can't be audited.
class RubricItemGrade(TypedDict):
    item_id: str
    score: int
    justification: str


class RubricGrade(TypedDict):
    items: list[RubricItemGrade]
    overall_assessment: str
    confidence: NotRequired[Literal["low", "medium", "high"]]


_DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"
_GRADER_IMAGE = "python:3.13-slim"

# Sample.metadata key carrying the host directory whose contents will be
# bind-mounted into the grader at /grade/recovered/. Eval task: the
# `_snapshot_work_dir` solver sets this after the agent runs. Validation
# tasks: pre-populated at task construction.
_RECOVERED_DIR_META = "recovered_host_dir"

_GRADER_INSTRUCTIONS = dedent(
    """\
    You are grading a reverse-engineering attempt against a fixed rubric.

    Your container has these read-only paths:

      /grade/recovered/         the agent's recovered Go (may be empty)
      /grade/reference/         the reference / ground-truth Go source
      /grade/spec/SPEC.md       externally-promised behavior of the binary
      /grade/rubric/RUBRIC.yaml the rubric you must grade against

    Workflow:
      1. Read RUBRIC.yaml and SPEC.md once.
      2. `find /grade/recovered -type f` to see what the agent produced.
      3. For each rubric item, compare recovery vs reference (typical
         moves: cat, diff -u, grep, wc -l). Identifier names do NOT matter —
         behavioral / structural correctness does.
           0 = none / wrong / missing
           1 = partial: high-level shape right but missing details that
               would prevent bit-identical reproduction
           2 = full: a faithful re-implementation of the agent's output
               would produce identical observable behavior
      4. Once you have grades for every rubric item, call `submit_grade`
         exactly ONCE. Justifications must cite which reference file vs
         which (or absent) recovered file you compared. Do not call
         `submit_grade` more than once.
    """
)
_GRADER_FIRST_USER = (
    "Begin grading. Read /grade/rubric/RUBRIC.yaml and /grade/spec/SPEC.md first, "
    "then explore /grade/recovered/. Call submit_grade once you've assigned a "
    "0/1/2 to every rubric item."
)


class _GraderContainer:
    """Manage a fresh docker container for grading.

    Started on `__aenter__` (`docker run -d --rm ...`), exec'd into via
    `exec_text(cmd)`, stopped on `__aexit__`. All mounts are read-only;
    the container can't be used to mutate anything host-side. `--rm`
    means even an orphaned container is GC'd by docker on exit.
    """

    def __init__(
        self,
        *,
        recovered_dir: Path,
        reference_dir: Path,
        spec_path: Path,
        rubric_path: Path,
        image: str = _GRADER_IMAGE,
    ) -> None:
        self._mounts: list[tuple[Path, str]] = [
            (recovered_dir, "/grade/recovered"),
            (reference_dir, "/grade/reference"),
            (spec_path, "/grade/spec/SPEC.md"),
            (rubric_path, "/grade/rubric/RUBRIC.yaml"),
        ]
        self._name = f"re_eval_grader_{uuid.uuid4().hex[:10]}"
        self._image = image

    async def __aenter__(self) -> _GraderContainer:
        cmd = ["docker", "run", "-d", "--rm", "--name", self._name]
        for src, dst in self._mounts:
            cmd += ["-v", f"{src.resolve()}:{dst}:ro"]
        cmd += [self._image, "tail", "-f", "/dev/null"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"docker run failed (rc={proc.returncode}): {stderr.decode('utf-8', errors='replace')}")
        logger.info("grader container %s started (id=%s)", self._name, stdout.decode().strip()[:12])
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "stop",
                "-t",
                "1",
                self._name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                logger.warning("grader container %s stop returned rc=%s", self._name, proc.returncode)
        except Exception:
            logger.exception("grader container %s teardown failed", self._name)

    async def exec_text(self, command: str, *, deadline_s: int = 120) -> str:
        """Run `bash -lc command` in the container; return stderr+stdout.

        Mirrors Inspect's `bash` tool semantics: stderr first, stdout
        second, joined into a single string. Hard-truncated at 64 KB so
        a runaway `cat` doesn't blow the judge context.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self._name,
            "bash",
            "-lc",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(deadline_s):
                stdout_b, stderr_b = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"(command timed out after {deadline_s}s)"
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        out = f"{stderr}\n{stdout}" if stderr else stdout
        if len(out) > 65536:
            out = out[:65536] + "\n... (truncated at 64 KB)"
        return out


def _grader_bash_tool(box: _GraderContainer) -> Tool:
    """`bash` tool that exec's into the grader container.

    Same shape as Inspect's stock `bash` tool, just pointed at our
    out-of-band docker container instead of the per-sample sandbox.
    """

    @tool
    def grader_bash() -> Tool:
        async def execute(command: str) -> str:
            """Run a bash command in the read-only grader container.

            Available paths: /grade/recovered, /grade/reference,
            /grade/spec, /grade/rubric. Stdout and stderr are returned
            together. Container is `python:3.13-slim` with standard
            tools (find, cat, diff, grep, wc, head, tail).

            Args:
                command: The bash command to execute.
            """
            return await box.exec_text(command)

        return execute

    return grader_bash()


def _submit_grade_tool(captured: dict[str, RubricGrade]) -> ToolDef:
    """Forced submit tool that captures the validated grade payload.

    `react()` calls this when the judge issues `submit_grade(...)`. We
    validate the args via TypeAdapter(RubricGrade) and stash them in the
    `captured` dict — the scorer reads them after the react loop returns.
    A schema mismatch raises (the loop catches it and asks the model to
    retry; if it never gets it right, the run errors out instead of
    silently passing a malformed grade through).
    """

    @tool
    def submit_grade() -> Tool:
        async def execute(items: list[dict], overall_assessment: str, confidence: str | None = None) -> str:
            """Submit per-item grades for the rubric. Call exactly ONCE.

            Args:
                items: One RubricItemGrade per rubric item id, each with
                    `item_id` (string), `score` (0/1/2), and `justification`
                    (string). Submit one entry per `id` in RUBRIC.yaml.
                overall_assessment: 3-6 sentence narrative summary.
                confidence: Optional grader confidence: low / medium / high.
            """
            payload: dict = {"items": items, "overall_assessment": overall_assessment}
            if confidence is not None:
                payload["confidence"] = confidence
            try:
                captured["grade"] = TypeAdapter(RubricGrade).validate_python(payload)
            except ValidationError as ex:
                raise RuntimeError(f"submit_grade payload failed schema validation: {ex}") from ex
            return "Grade recorded. The session is complete."

        return execute

    return ToolDef(submit_grade(), name="submit_grade")


def _resolve_recovered_dir(state: TaskState) -> Path:
    raw = state.metadata.get(_RECOVERED_DIR_META)
    if not raw:
        raise RuntimeError(
            f"Sample {state.sample_id!r} missing metadata['{_RECOVERED_DIR_META}']. "
            "The eval task should run `_snapshot_work_dir` after the agent; "
            "validation tasks must set it at construction."
        )
    p = Path(raw)
    if not p.is_dir():
        raise RuntimeError(f"recovered_host_dir does not exist or is not a directory: {p}")
    return p


@scorer(metrics=[accuracy(), stderr()])
def rubric_judge(*, judge_model: str = _DEFAULT_JUDGE_MODEL, max_messages: int = 80) -> Scorer:
    """LLM-graded rubric scorer running in its own grader container.

    Per sample:
      1. Resolve the agent's recovered host dir from `state.metadata`.
      2. Spin up a fresh grader docker container with rubric / spec /
         reference / recovered all bind-mounted read-only.
      3. Drive a `react()` loop inside it with `bash` + a forced
         `submit_grade`. Tool calls land in the eval log so each grade
         is auditable.
      4. Pull the captured `RubricGrade` out, compute weighted
         normalized score (Inspect convention: 0.0-1.0).

    `Score.metadata['per_item']` carries the full `{item_id, score,
    justification}` dict; per-item metrics can be added later.
    """
    rubric_yaml_text = get_required_path(_RUBRIC_RLOCATION).read_text()
    rubric = yaml.safe_load(rubric_yaml_text)
    weights = {it["id"]: it["weight"] for it in rubric["items"]}
    weight_total = sum(weights.values())
    rubric_path = get_required_path(_RUBRIC_RLOCATION)
    spec_path = get_required_path(_SPEC_RLOCATION)
    reference_dir = get_required_path(_SPECIMEN_DIR_RLOCATION)

    async def score(state: TaskState, target: Target) -> Score:
        recovered_dir = _resolve_recovered_dir(state)
        # Snapshot the agent's `/work/` into `recovered_dir` HERE rather than
        # in a post-agent solver: Inspect aborts the solver list on
        # `LimitExceededError`, so a time-limited agent never reaches a
        # post-agent solver. The scorer runs in its own time_limit/2
        # context regardless of how the agent ended, and the per-sample
        # docker sandbox is still alive at this point. For validation
        # tasks `recovered_dir` is pre-populated and there's no live
        # sandbox; `_snapshot_into` is a no-op when `sandbox()` is unset.
        await _snapshot_into(recovered_dir)

        captured: dict[str, RubricGrade] = {}
        async with _GraderContainer(
            recovered_dir=recovered_dir, reference_dir=reference_dir, spec_path=spec_path, rubric_path=rubric_path
        ) as box:
            grader_agent = react(
                name="rubric_grader",
                description="Grades a reverse-engineering attempt.",
                prompt=AgentPrompt(instructions=_GRADER_INSTRUCTIONS),
                tools=[_grader_bash_tool(box)],
                submit=AgentSubmit(tool=_submit_grade_tool(captured)),
                model=judge_model,
            )
            try:
                await grader_agent(AgentState(messages=[ChatMessageUser(content=_GRADER_FIRST_USER)]))
            except Exception:
                if "grade" not in captured:
                    raise

        if "grade" not in captured:
            raise RuntimeError(f"Grader did not call submit_grade after {max_messages} messages.")
        payload = captured["grade"]

        # Weighted sum: per-item score in {0,1,2} -> fraction of item's weight.
        # Unknown ids are ignored; missing ids count as 0. Both surface in metadata.
        per_item = {g["item_id"]: g for g in payload["items"]}
        weighted = 0.0
        missing = [k for k in weights if k not in per_item]
        unknown = [k for k in per_item if k not in weights]
        for item_id, weight in weights.items():
            graded = per_item.get(item_id, {"score": 0})["score"]
            weighted += weight * graded / 2
        normalized = weighted / weight_total

        return Score(
            value=normalized,
            answer=(state.output.completion or "")[:2000],
            explanation=payload["overall_assessment"],
            metadata={
                "per_item": per_item,
                "weights": weights,
                "raw_score_pct": round(weighted, 2),
                "missing_items": missing,
                "unknown_items": unknown,
                "judge_model": judge_model,
                "confidence": payload.get("confidence"),
                "recovered_host_dir": str(recovered_dir),
            },
        )

    return score


def _resolve_snapshot_root() -> Path:
    """Host directory under which per-sample `/work/` snapshots land.

    Read from `$RE_EVAL_SNAPSHOT_DIR` (set by run.py to the eval log dir
    so snapshots live next to the .eval log). Falls back to a fresh
    tempdir for ad-hoc `inspect eval ...` invocations.
    """
    root_env = os.environ.get(_SNAPSHOT_DIR_ENV)
    return Path(root_env) if root_env else Path(tempfile.mkdtemp(prefix="re_eval_snapshot_"))


async def _snapshot_into(out_dir: Path) -> None:
    """Tar the agent's `/work/` out of the active per-sample sandbox.

    Same idiom SWE-bench uses: stage a tarball inside the sandbox via
    `sandbox().exec(["tar", ...])`, pull bytes out with
    `sandbox().read_file(text=False)`, extract on the host.

    Called from inside the scorer (rubric_judge) rather than as a
    post-agent solver. Inspect aborts the solver list on
    `LimitExceededError`, so a time-limited agent never reaches a
    post-agent solver — but the scorer always runs, in its own
    `time_limit/2` context, with the per-sample sandbox still alive.

    No-op when there is no active sandbox (validation tasks, where
    `out_dir` is pre-populated at task construction).
    """
    try:
        sb = sandbox()
    except Exception:
        # No per-sample sandbox active — validation tasks. The dir is
        # already populated by the task; nothing to do.
        return

    try:
        in_box = "/tmp/re_eval_work.tar"
        result = await sb.exec(["tar", "-C", _WORK_PATH, "-cf", in_box, "."])
        if result.returncode != 0:
            logger.warning("work snapshot tar failed: rc=%s stderr=%s", result.returncode, result.stderr)
            return
        data = await sb.read_file(in_box, text=False)
    except Exception:
        logger.exception("work snapshot failed")
        return

    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(out_dir, filter="data")
    except Exception:
        logger.exception("work snapshot extract failed; falling back to tarball at %s/work.tar", out_dir)
        (out_dir / "work.tar").write_bytes(data)


@task
def reverse_engineer_go_crypto(
    *, message_limit: int = 1000, time_limit: int = 43200, judge_model: str = _DEFAULT_JUDGE_MODEL
) -> Task:
    """Recover compilable Go source from a garbled go_crypto_server binary."""
    skill_dir = _stage_skill()
    target_binary = _stage_target_binary()
    compose_path = get_required_path(_COMPOSE_RLOCATION)

    skill_md = (skill_dir / "SKILL.md").read_text()
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(skill_md=skill_md, skill_path=_SKILL_PATH)

    # Pre-stamp the recovered-source dir BEFORE the agent runs, so a
    # time-limited / message-limited / errored agent still leaves the
    # judge a (possibly empty) dir to grade — Inspect aborts the solver
    # list on `LimitExceededError`, which means `_snapshot_work_dir`
    # may never run, and we don't want that to crash the scorer.
    sample_id = "go_crypto_server"
    recovered_dir = _resolve_snapshot_root() / f"work_{sample_id}"
    recovered_dir.mkdir(parents=True, exist_ok=True)

    sample = Sample(
        id=sample_id,
        input=_FIRST_USER_MESSAGE,
        files={f"{_INPUT_PATH}/target": str(target_binary), f"{_SKILL_PATH}": str(skill_dir)},
        metadata={"specimen": "go_crypto_server", "skill": "reverse_engineer", _RECOVERED_DIR_META: str(recovered_dir)},
    )

    submit_tool = ToolDef(
        submit_summary(), name="submit", description="Call when reverse engineering is complete (or you are stuck)."
    )

    # react() drives a tool-use loop until the agent calls `submit`. Our
    # custom submit tool returns the summary as the answer; AgentPrompt's
    # default assistant_prompt + submit_prompt are kept so the model gets
    # the standard ReAct framing alongside our skill block. bash() runs in
    # the docker sandbox declared on the task; the same `timeout` value is
    # used for every command (long apt-get installs use it too).
    agent = react(
        prompt=AgentPrompt(instructions=system_prompt), tools=[bash(timeout=180)], submit=AgentSubmit(tool=submit_tool)
    )

    return Task(
        dataset=MemoryDataset([sample]),
        # Just the agent. The /work/ snapshot is taken inside rubric_judge
        # rather than as a post-agent solver, because Inspect aborts the
        # solver list on LimitExceededError — meaning a time-limited
        # agent would skip a post-agent solver. The scorer runs even
        # then, in its own time_limit/2 context.
        solver=as_solver(agent),
        scorer=rubric_judge(judge_model=judge_model),
        sandbox=SandboxEnvironmentSpec(type="docker", config=str(compose_path)),
        message_limit=message_limit,
        time_limit=time_limit,
    )


# --- Validation tasks --------------------------------------------------------
#
# These bypass the agent entirely. There is no docker sandbox — the scorer's
# grader container is the only container in play. The task pre-populates
# `recovered_host_dir` on Sample.metadata; the rubric_judge mounts that as
# /grade/recovered/ in its grader container.
#
#   - validate_empty_work       expected ~0.0 (judge floor)
#   - validate_reference_work   expected >0.85 (judge ceiling)
#
# If empty scores high or reference scores low, the judge prompt or the
# rubric is broken — fix that before trusting any agent score.


@solver
def _identity_solver() -> Solver:
    """Solver that does nothing. Validation tasks have no agent; this lets
    Inspect run a sample whose only computation is the scorer."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        return state

    return solve


def _empty_recovered_dir() -> Path:
    """Fresh empty dir to bind-mount as the empty-work `/grade/recovered/`."""
    return Path(tempfile.mkdtemp(prefix="re_eval_empty_recovered_"))


def _reference_recovered_dir() -> Path:
    """Reference Go source copied into a fresh dir.

    We don't mount the specimen runfiles directly because that dir also
    contains `RUBRIC.yaml`, `SPEC.md`, `BUILD.bazel`, `test_smoke.py` —
    things a real agent would never produce, and we don't want the judge
    to discover the rubric inside the recovery dir. Copy only the *.go
    files; that's what a well-graded recovery should look like.
    """
    src = get_required_path(_SPECIMEN_DIR_RLOCATION)
    dst = Path(tempfile.mkdtemp(prefix="re_eval_reference_recovered_"))
    for f in sorted(src.glob("*.go")):
        shutil.copy(f, dst / f.name)
    return dst


def _validation_task(*, sample_id: str, recovered_dir: Path) -> Task:
    """Common task body for the two validation variants.

    No docker sandbox is declared — the scorer's grader container is the
    only container that runs. The recovered dir path is passed via
    Sample.metadata; rubric_judge resolves it.
    """
    sample = Sample(
        id=sample_id,
        input="(validation; no agent; rubric_judge grades the pre-seeded recovered dir)",
        metadata={"specimen": "go_crypto_server", "validation": True, _RECOVERED_DIR_META: str(recovered_dir)},
    )
    return Task(
        dataset=MemoryDataset([sample]),
        solver=[_identity_solver()],
        scorer=rubric_judge(),
        # No agent runs, so no message_limit. We deliberately omit
        # time_limit too: Inspect splits `time_limit` into `time_limit/2`
        # for scoring (`_eval/task/run.py`), which strangles the judge
        # for the reference case. The scorer has its own per-bash-call
        # `deadline_s`; that's the right place to bound runtime.
        message_limit=4,
    )


@task
def validate_empty_work() -> Task:
    """Judge floor: empty `/grade/recovered/`. Expected score ~0.0."""
    return _validation_task(sample_id="empty_work", recovered_dir=_empty_recovered_dir())


@task
def validate_reference_work() -> Task:
    """Judge ceiling: `/grade/recovered/` contains the reference *.go.
    Expected score >0.85."""
    return _validation_task(sample_id="reference_work", recovered_dir=_reference_recovered_dir())
