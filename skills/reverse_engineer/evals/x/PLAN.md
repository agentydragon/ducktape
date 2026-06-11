# Reverse-engineering eval — Inspect AI port

Port of the manually-scored Agent Framework eval at
`skills/reverse_engineer/evals/agent_framework/re_rollout.py` to
Inspect AI: react agent recovers Go source from a `garble`-obfuscated
binary, an LLM judge grades the recovery against
`evals/tasks/go_crypto_server/RUBRIC.yaml`.

## What is here

| File                | Purpose                                                                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task.py`           | `@task reverse_engineer_go_crypto`, `validate_empty_work`, `validate_reference_work`, the `rubric_judge` scorer, and the `_GraderContainer` that hosts it. |
| `run.py`            | `bb run :run` entry point — runs the agent eval against a chosen `--model`.                                                                                |
| `validate_judge.py` | `bb run :validate_empty` / `:validate_reference` — judge floor/ceiling sanity checks (no agent).                                                           |
| `_runner.py`        | Shared CLI plumbing: credential check, log-dir defaulting, `inspect_eval()` call.                                                                          |
| `compose.yaml`      | Agent docker sandbox: `python:3.13-slim`, `working_dir=/work`.                                                                                             |
| `BUILD.bazel`       | `:task` library + four `py_binary` entrypoints (`run`, `validate_judge`, `validate_empty`, `validate_reference`).                                          |

## Architecture

### Agent sandbox

`reverse_engineer_go_crypto` runs `react()` inside a docker compose
sandbox declared on the task. The agent has `bash(timeout=180)` and a
custom `submit(summary: str)` tool. `Sample.files` copies the garbled
binary to `/input/target` and the staged `reverse_engineer` skill tree
to `/skill/`; `/work/` is the agent's scratch space.

After the react loop ends (submit, message limit, or time limit), the
`_snapshot_work_dir` solver tars `/work/` out via
`sandbox().exec(["tar", ...])` + `sandbox().read_file(text=False)` —
the SWE-bench idiom. The extracted tree lands at
`<log_dir>/work_<sample_id>/` and its host path is stamped into
`state.metadata["recovered_host_dir"]`.

### Judge sandbox (separate container)

The agent must never see the reference source. So `rubric_judge` spins
up its own grader container via plain `docker run -d --rm` with four
read-only bind mounts: `/grade/recovered/` (snapshot dir),
`/grade/reference/` (specimen `*.go`), `/grade/spec/SPEC.md`,
`/grade/rubric/RUBRIC.yaml`. Inside that container the scorer drives a
second `react()` loop with a `bash` tool that exec's into the grader
and a forced `submit_grade(items, overall_assessment, confidence?)`
tool whose payload is `TypeAdapter(RubricGrade)`-validated. Per-item
`{score: 0|1|2, justification}` plus weighted normalized total land in
`Score.metadata["per_item"]` / `Score.value`. Token cost is dominated
by the judge actually `cat`/`diff`-ing the files it cares about, not
by prompt-stuffing 800 KB of source.

### Validation entry points

`validate_empty_work` and `validate_reference_work` skip the agent
entirely. They construct a Sample whose `metadata["recovered_host_dir"]`
points at either an empty tempdir or a fresh tempdir containing only
the reference `*.go` files (no rubric/spec/build files). Used to
sanity-check the judge:

- empty → 0.000 (verified 2026-04-29, Sonnet 4.6, high confidence)
- reference → 0.940 (verified 2026-04-29, Sonnet 4.6, high
  confidence; 11/12 items at 2/2; the original `mac_security_analysis`
  item scored 0/2 because the reference `*.go` is just code, no
  analytical commentary, so we removed that item from the rubric to
  keep it focused on source reconstruction)

If empty ever scores high or reference scores low, fix the judge
prompt or the rubric before trusting any agent score.

## Inspect-AI specifics worth knowing

- **Prompt caching**: auto-enabled by Inspect's Anthropic provider
  (`cache_control: ephemeral` on system + tools) for caching-eligible
  Claude models. Nothing to configure.
- **Strict tool use**: auto-enabled for `openai-api/` provider tools
  (`strict: true` on every custom tool's input schema). NOT yet wired
  for the native Anthropic provider — adding a `_StrictAnthropicClient`
  subclass that injects the field is a followup, mirroring
  `skills/eval_infra/af_chat_client.py`.
- **`time_limit` is split**: Inspect's `_eval/task/run.py` uses
  `scoring_time_limit = time_limit / 2`, so the scorer gets half the
  task's wall budget. With `--time-limit 1200` the agent and the
  judge each get ~10 min; tune up if the judge times out on rich
  recoveries.
- **No `:view` Bazel target**: Inspect's view server mounts its
  frontend `dist/` via Starlette `StaticFiles`, which `realpath`s
  every request and rejects runfiles symlinks. Use `env -u PYTHONPATH
uvx --from inspect-ai inspect view start --log-dir <log_dir>`
  instead — the `.eval` files are self-contained.

## Followups

- **Anthropic strict tool mode** (above).
- **Stage 3 — differential verification**: compile recovered Go in
  the grader container, run `evals/tasks/go_crypto_server/test_smoke.py`
  against the agent's binary, surface a sub-score for "actually builds
  and matches the reference at the protocol level."
- **Multi-specimen support**: rubric per specimen, dataset of more
  than one Sample. Specimen plans live in
  `skills/reverse_engineer/evals/TODO.md`.
- **Judge determinism check**: two runs against the same recovered
  tree should score within ~5 pp. Not yet measured.
- **Token cost optimization for the judge**: not currently a problem
  but the prompt-stuffing alternative does exist if container startup
  becomes the bottleneck.

## Out of scope

- Resuming a partial run from a transcript. Run end-to-end or fail.
- Surfacing the eval in a custom UI / dashboard. Inspect's stock log
  viewer is enough.
