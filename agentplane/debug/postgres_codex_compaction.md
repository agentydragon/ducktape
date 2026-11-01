# Postgres sampler compaction

The test-only `x/postgres_codex` sampler added an explicit compaction transition and an opt-in automatic
trigger. Its source and Bazel targets have since been removed; this report preserves the historical
findings. Conversation history and provider/context history had separate storage: the original
items and intent evidence remain readable after context replacement. The process registry foundation
is shared with the independently implemented process controls; this slice regenerates handles from
that registry and refuses live cells.

## Pinned protocol

The repository pin is Codex 0.162.1 (`rust-v0.162.1`, source commit `092d3acd`). Its
[`compact_remote_v2_attempt.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/compact_remote_v2_attempt.rs)
adds a `compaction_trigger` to the ordinary Responses input. The
[`compact_remote_v2.rs`](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/compact_remote_v2.rs)
collector requires completion and exactly one `compaction` output; its replacement history retains
selected user goals/developer context alongside that item. The native controller also supports a local
natural-language summary path in `compact.rs`; this experiment implements only remote v2, without
fallback or automatic provider retry.

The upstream
[`compact_remote.rs` tests](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/tests/suite/compact_remote.rs)
assert POST `/v1/responses`, the `remote_compaction_v2` beta header, the trigger item, and opaque summary
continuation. They also emit `response.output_item.done` followed by a completed event that has no
output array. The test-local endpoint here uses Agentplane's queued model-exchange fixture while
preserving unknown dictionary fields; it scripts that same wire shape. Runtime code does not decode,
re-encode or project the opaque item's fields. PostgreSQL JSONB retains JSON semantics, not wire-byte
formatting or object key order.

This minimum replaces the old context with the opaque item alone, regenerating the current standing
instructions/tools/process handles outside it. It does not implement native retained-user-goal budgets,
image retention, dynamic instruction discovery, model fallback or account negotiation. The encrypted
summary is the model's representation of prior goals and tool results. The original unmodified data
remains in the audit items and intents.

## Verification

The RBE integration suite uses real PostgreSQL 18 and pinned exec-server/code-mode-host binaries.
It checks a committed compaction intent visible to the provider; a smaller context; unchanged opaque
fields; fresh sampler processes that execute a real tool after compaction; and another sample using
the tool result. A separate real exec-server process remains live while fresh compaction and sampler
processes regenerate its registry identity. Negative cases cover stream truncation, terminal failure,
tool-returning summaries, oversized/duplicate summaries, task cancellation, lease loss, unresolved
cell/nested intents and orphan calls/results. The automatic trigger checks the last completed provider
usage and does not restart an assistant-final conversation.

The existing native compaction standing-instruction regression target passed from cache in RBE
invocation `8aca6e91-bd97-4f2a-8836-0bd1c82894d8` (11.6 seconds); that native fixture selects its local
summary path. The custom compaction suite's 15 cases and original sampler's seven cases passed in RBE invocation
[`f84ec0d4-a55f-44d8-b7ad-7f702cb87bd5`](https://app.buildbuddy.io/invocation/f84ec0d4-a55f-44d8-b7ad-7f702cb87bd5)
(31.5 and 30.2 seconds respectively), alongside explicitly named changed runtime libraries' Ruff/mypy
aspects. These are scripted protocol/controller checks, with no live inference or provider acceptance
claim.

Failure is deliberately sticky: usable pre-compaction context remains stored, but an unresolved intent
blocks unattended continuation. There is no operator reconciliation interface. Automatic compaction is
an observed-usage trigger, not protection against arbitrary tool-output growth; canonical payload bytes
prove the installed context shrank without claiming a token estimate.
