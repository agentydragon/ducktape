# TODO

## Standards

- Potential indexing (property-specimen cross-refs) if/when scale requires it
- Policy: should verbatim docstring repetition in ABC subclass methods violate no-useless-docs? Lean yes, undecided.
- Property naming mismatch: "self-describing names" vs "use datetime for datetimes". Decide scope or split.
- Target Python version detection/guidance for agents/graders

## Features

- Reimplement `fix` command as critic-driven loop: run critic, fix issues, rerun until clean or max iterations
- Agent timeout warning handler: inject "5 minutes remaining" messages using `created_at` + `timeout_seconds`

## Infrastructure

- Sane story for applying migrations without full `db recreate` (direct `alembic upgrade head`)
- Bulk specimen sync in `props db sync` from Bazel bundle artifacts (currently one-by-one via `sync-specimen`)
- LLM API shape: props is OpenAI-Responses-only (`backend/routes/llm.py` proxies `/v1/responses`). z.ai (no native `/responses`) only works through LiteLLM's `use_chat_completions_api` chat→Responses bridge, which mislabels GLM reasoning content as `output_text` (worked around by `ReasoningOutputTextItem` in `openai_utils/model.py`). Find a better fix: a LiteLLM config/version that emits `reasoning_text`, or teach props to speak `chat/completions` directly so reasoning doesn't round-trip the lossy Responses bridge.
