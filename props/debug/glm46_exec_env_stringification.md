# glm-4.6 stringifies optional tool-call args → 100% `exec` failure

**Date:** 2026-06-04
**Status:** mitigated (root knob removed); upstream cause not fully isolated — see Follow-up.

## Symptom

A live critic run on `glm-4.6` (z.ai via LiteLLM), run id `824e8815-fbd3-44a4-b57d-6f059d84df83`,
produced garbage: **all 55 `exec` tool calls failed**, so the agent never read a single file. Its 3
"issues" were 2 meta-complaints about `exec` being broken + 1 hallucination (`hardcoded-api-credentials`
at a line it never read) → ~0 grading credit. This is why `glm-4.6` critics had ~0 recall.

Every `exec` call returned the same validation error:

```json
[{ "type": "list_type", "loc": ["env"], "msg": "Input should be a valid list", "input": "null" }]
```

The model sent the optional `env` arg as a JSON-encoded **string** — `"null"`, `"[]"`,
`'["PATH=/usr/bin:/bin"]'` — instead of native JSON `null` / `[]` / `[...]`. (It also stringified
`stdin_text` to `"null"`, which _silently passed_ as the literal string, since that field is `str | None`.)

## Root cause

- `DirectExecArgs.env` was `list[EnvVar] | None` (strict, `extra="forbid"`). A `str` is not a `list` → rejected.
- The critic's tools are sent with OpenAI **strict** tool-calling, which forces **every** field into
  `required` — so the model is compelled to emit the optional `env` on every call (it can't omit it),
  and `glm-4.6` fills the nullable value as a quoted string. Note `cmd` (a required, non-nullable
  `list[str]`) came through correctly as a native array — only the **nullable/optional** `env` (an
  `anyOf: [array, null]`) got stringified.

## Where the bug is NOT

The tool schema **we send** is correctly typed (verified from the logged request `tools[]`):

```json
"env": {"anyOf": [{"type": "array", "items": {"type": "string", "pattern": "^[^=]+=.*$"}}, {"type": "null"}], "default": null}
```

So props/agent*core is exonerated. The stringification is downstream — LiteLLM's Responses-API↔z.ai
translation, or z.ai/GLM ignoring the `anyOf`+strict schema. (`function_call.arguments` is a JSON
string the model produces and LiteLLM passes through ~verbatim, which \_leans* toward the model
producing `env="null"` — but LiteLLM also re-translates the tool **schema** before z.ai sees it, so
it isn't fully isolated.)

## Mitigation applied

- **Removed the `env` / `inherit_env` knob from `DirectExecArgs`** (`mcp_infra/exec/subprocess.py`).
  Direct exec now always inherits the ambient environment; nothing set `env` on direct exec, and
  agents reviewing code never need it. The model no longer sees the field, so it can't trip on it.
  Regression test: `mcp_infra/exec/test_direct.py::test_direct_exec_omits_env_knob`.
- **Generalized the critic's `report_failure`** into an explicit escape hatch (tool docstring +
  `prompt.md.mako`): call it when blocked by tooling/environment/validation instead of hallucinating
  or submitting a partial critique. (Not for "can't run/build the code" — review is static.)

## Follow-up (not done)

1. **Isolate LiteLLM vs z.ai/GLM.** Call LiteLLM directly with a minimal tool that has a nullable
   param, model `glm-4.6`, comparing `/v1/responses` vs `/v1/chat/completions` — does only the
   Responses path stringify? Or enable LiteLLM debug logging / inspect Langfuse to capture the exact
   z.ai-bound request (translated schema) + raw response.
2. **Broader risk:** `glm-4.6` stringified `stdin_text` too (silently passed). Other tools with
   optional/nullable args (grader, critic_dev) may carry subtle silently-wrong values under strict
   tool-calling on this model. Consider a general mitigation: coerce stringified args at the
   `agent_core` arg-parse boundary, and/or avoid forcing optional/nullable fields into `required` in
   strict tool schemas.
3. **Observability:** `GET /api/runs/{id}/logs` (Loki) currently times out (separate note) — it would
   have surfaced this failure far faster than reconstructing it from the transcript.
