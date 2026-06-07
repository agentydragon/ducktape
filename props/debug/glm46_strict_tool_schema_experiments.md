# glm-4.6 strict tool schema experiments

**Date:** 2026-06-06

## Context

Props critic direct exec was failing on `glm-4.6` because the model returned stringified values for
nullable required tool fields. The important exec fields were:

- `exec.env`: previously `list[EnvVar] | None`; z.ai returned `"null"`, `"[]"`, or a stringified
  JSON array instead of a native JSON null/list.
- `exec.stdin_text`: previously `str | None`; z.ai returned `"null"`, which passed validation as a
  literal stdin string.

One live critic run (`641e1a49-de5f-4338-8d19-f6db8be63b5a`) also produced an `exec.cmd` value as a
stringified argv list and a malformed tool-call name resembling `python3</arg_value>`. Minimal
experiments did not reproduce a general failure for required non-null `list[str]`, so treat that as a
weaker/model-quality symptom rather than the primary schema-shape bug.

## Experiment

Tested `glm-4.6` against both:

- z.ai coding Chat Completions directly (`https://api.z.ai/api/coding/paas/v4/chat/completions`)
- cluster LiteLLM Chat Completions forwarding to z.ai

Both paths behaved the same for the relevant schema shapes.

## Results

Working shapes:

- Required `string` emitted a native JSON string.
- Required `array` of strings emitted a native JSON array.
- Required `array` of command-like strings, including pipe tokens inside `"sh -c"` argv, emitted a
  native JSON array.
- Optional nullable field omitted from `required` was omitted when not needed.
- Required non-null sentinel shapes worked:
  - empty-string sentinel for stdin-like text
  - empty-array sentinel for env-like lists
  - object wrapper such as `{"mode": "inherit", "values": []}`
  - enum sentinel such as `"none"`

Broken shapes:

- Required nullable string (`anyOf: [{"type": "string"}, {"type": "null"}]`) returned `"null"` as a
  string when asked to pass JSON null.
- Required nullable array (`anyOf: [{"type": "array"}, {"type": "null"}]`) returned `"null"` as a
  string when asked to pass JSON null.
- Required nullable array returned a stringified JSON array when asked to pass a list.

## Decision

For direct props exec, remove model-controlled optional/nullable args instead of trying to coerce
them. `env` was already removed; `stdin_text` was removed after this experiment. If a future tool
needs these concepts, prefer non-null sentinel shapes or an explicit object with a mode discriminator,
and validate that exact shape against z.ai before deploying it.

## Follow-up: object-typed union parameters are stringified (2026-06-07)

The same `anyOf` weakness applies to **object** parameters, not just nullable scalars/arrays.

A `critic_dev_optimize` run (`a4cb7710-d9d8-424b-9264-9c7ef8845a25`, glm-4.6, $50 budget) failed
(`exit 1`, only $1.53 spent) because **every** `start_critic` call stringified its `example`
argument. `start_critic`'s `example` is the props `ExampleSpec` discriminated union, rendered as
`anyOf: [WholeSnapshotExample, SingleFileSetExample]` with a `discriminator`. glm-4.6 emitted:

```json
"example": "{\"kind\": \"file_set\", \"snapshot_slug\": \"...\", \"files_hash\": \"...\"}"
```

i.e. a JSON-encoded **string** where the schema wants an object. The top-level `arguments` blob is
still valid JSON (a naive "is it valid JSON" check passes), but Pydantic rejected the field with
`model_attributes_type` — "Input should be a valid dictionary or object to extract fields from". The
agent retried 8×, reshuffling keys and bumping budget/timeout but never switching the string to an
object, then gave up via `report_failure`, mis-attributing it to a "tool compatibility issue".

### Experiment

Replayed `start_critic`'s tool schema against the z.ai coding endpoint (3 samples each,
`temperature: 0`), varying only the `example` parameter's schema shape:

| Variant                                                                    | `example` returned |
| -------------------------------------------------------------------------- | ------------------ |
| real schema (`anyOf` + `$ref`/`$defs` + discriminator, `strict: true`)     | STR ✗ (3/3)        |
| same but `strict: false`                                                   | STR ✗ (3/3)        |
| `anyOf` inlined (no `$ref`/`$defs`)                                        | STR ✗ (3/3)        |
| `oneOf` union (instead of `anyOf`)                                         | STR ✗ (3/3)        |
| single concrete object, `const` kind                                       | OBJ ✓ (3/3)        |
| single concrete object, multi-value `enum` kind (superset of fields)       | OBJ ✓ (3/3)        |
| single concrete object, `additionalProperties: true`                       | OBJ ✓ (3/3)        |
| fully-flat top-level params (`example_kind`, `example_snapshot_slug`, ...) | OBJ ✓ (3/3)        |

The trigger is the **union combinator** (`anyOf` _and_ `oneOf`) — independent of `strict`,
`$ref`/`$defs`, and the discriminator. Any single concrete object schema (with defined `properties`)
or a fully-flat parameter list works.

This is a **GLM model-level bug**, not a z.ai-API quirk: it reproduces on both the z.ai API and
OpenCode Zen, and other models (Claude, MiniMax) handle the same unioned tools fine. GLM emits tool
calls as **XML** (`<parameter name="x">…</parameter>`); the XML→arguments parser stringifies a tag's
content unless the schema declares a single concrete type, so a plain `object` is JSON-parsed back to
an object while an `anyOf`/`oneOf`/`allOf` union is type-ambiguous and left as a raw string. See
[zai-org/GLM-4.7 discussion #18](https://huggingface.co/zai-org/GLM-4.7/discussions/18) (same bug on
GLM-4.7's Notion `update-page` `allOf`/`anyOf` schema; open/unresolved as of 2025-12). z.ai's
[function-calling docs](https://docs.z.ai/guides/capabilities/function-calling) are examples-only and
document no schema-feature constraints beyond `tool_choice` "only supports auto".

### Decision

For z.ai tool inputs, represent a discriminated union as a **single concrete object** schema (carry
the superset of fields, keep `kind` as an `enum` over the variants, and enforce the per-`kind`
required fields server-side after validation) — or as flat top-level params. Never `anyOf`/`oneOf`.
Both working shapes are canaried live in `agent_core/test_zai_chat_adapter_live.py`
(`test_zai_object_tool_param_returned_as_object_live` passes; the `anyOf` union variant is `xfail`).
See also <../../docs/z_ai_api.md> "Tool Use / Function Calling".
