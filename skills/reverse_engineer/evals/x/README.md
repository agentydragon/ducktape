# Reverse-engineering eval — Inspect AI

Inspect AI port of the manually-scored Agent Framework eval at
`../agent_framework/`. A `react` agent recovers Go source from a
`garble`-obfuscated binary; an LLM judge grades it against
`../tasks/go_crypto_server/RUBRIC.yaml` in a separate grader container.

For the architecture and design rationale see <PLAN.md>.

## Files

| File                | Purpose                                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `task.py`           | `@task reverse_engineer_go_crypto`, `validate_empty_work`, `validate_reference_work`, the `rubric_judge` scorer. |
| `run.py`            | `bb run :run` entry — runs the agent eval against `--model`.                                                     |
| `validate_judge.py` | `bb run :validate_empty` / `:validate_reference` — judge floor/ceiling sanity checks (no agent).                 |
| `_runner.py`        | Shared CLI: credential check, log-dir defaulting, `inspect_eval()` call.                                         |
| `compose.yaml`      | Agent docker sandbox: `python:3.13-slim`, `working_dir=/work`.                                                   |
| `BUILD.bazel`       | `:task` library + `:run` / `:validate_judge` / `:validate_empty` / `:validate_reference` binaries.               |

## Run

```bash
# Eval — Haiku (default model)
bb run //skills/reverse_engineer/evals/x:run

# Eval — Sonnet
bb run //skills/reverse_engineer/evals/x:run -- --model anthropic/claude-sonnet-4-6

# OpenAI-compatible
bb run //skills/reverse_engineer/evals/x:run -- --model openai/gpt-4o-mini

# Watch the conversation in realtime instead of the plain summary
bb run //skills/reverse_engineer/evals/x:run -- --display conversation

# Judge floor (empty /grade/recovered/) — expected score ~0.0
bb run //skills/reverse_engineer/evals/x:validate_empty

# Judge ceiling (reference *.go in /grade/recovered/) — expected score >0.85
bb run //skills/reverse_engineer/evals/x:validate_reference
```

`bb run` builds on RBE and runs the binary locally. The Python process
needs to talk to a local docker daemon, which is why we use `bb run`
(not `bbr`) — the binary runs on the host where Docker is available.

Eval logs land in `./eval_logs/<utc-stamp>/`; judge-validation logs in
`./validate_judge_logs/<case>/<utc-stamp>/`. Both relative to
`BUILD_WORKING_DIRECTORY`.

## Inspecting logs

```bash
env -u PYTHONPATH uvx --from inspect-ai inspect view start --log-dir eval_logs
```

`uvx` resolves Inspect into a one-shot venv. The `env -u PYTHONPATH`
prefix is the same NixOS dodge `cluster/docs/inference/runs/.../run_aime.py`
uses — without it the Nix-store `pydantic` leaks into the uv venv and
clashes with the venv's `pydantic_core`. If `inspect-ai` is installed
globally (`uv tool install inspect-ai`), `inspect view start --log-dir …`
works the same way.

There is no Bazel `:view` target — Inspect's view server mounts its
frontend `dist/` via Starlette `StaticFiles`, which `realpath`s every
request and rejects runfiles symlinks. The `.eval` files are
self-contained; serve them with `uvx` instead.

## Status (2026-04-29)

- **Agent eval**: end-to-end runnable. Verified against
  `anthropic/claude-haiku-4-5-20251001` (last full run: 6:34 wall, 131
  messages, 2.3M tokens, cache-read:input ratio ~14000:1).
- **Rubric judge**: drives a second react loop in its own grader
  docker container; reads recovered/reference/spec/rubric over `bash`,
  emits a schema-validated `submit_grade` tool call. Per-item grades +
  justifications land in `Score.metadata['per_item']`.
- **Floor/ceiling validation**: empty → 0.000 (high confidence);
  reference `*.go` → 0.940 (high confidence). Both verified, both have
  Bazel targets.
- **Rubric**: 11 items, weights sum to 94. The original
  `mac_security_analysis` item (which asked whether the agent reasoned
  about a length-extension vulnerability) was removed — the rubric is
  now scoped to source reconstruction only.

## Followups

Tracked in <PLAN.md>:

- Anthropic strict tool mode (provider doesn't yet emit
  `strict: true` on custom tools — would need a `_StrictAnthropicClient`
  subclass mirroring `skills/eval_infra/af_chat_client.py`).
- Stage 3 differential verification — compile recovered Go in the
  grader, run `test_smoke.py` against the agent's binary.
- Multi-specimen support.
- Judge determinism check.
