# Auditing the conversation vocabulary for provider leakage

<../x/conversation_events.py> is the vocabulary every channel renders and every backend adapter
produces, and the invariant it exists to hold is that **no channel knows a provider's frame
shape**. A member that is really one provider's concept renamed breaks that invariant at one
remove: a channel rendering it is rendering Claude.

`ActivityStarted`/`ActivityCompleted` already failed that test — produced by exactly one arm
matching `case "task_started"` and reading `task_id`, `tool_use_id` and `description`, Claude's own
field names 1:1 — and are being retired. This asks the same question of every remaining member.

Read against `origin/devel` at `0869f6560a`, 2026-08-17. Step 13 of
`haku/console/plans/conversation_layers.md` § 9, on the unmerged branch `claude/haku-channel-layers`.

## The test

For each member: which `case` in <../x/claude_code/projection.py> produces it, which payload keys
that arm reads, and whether those keys are the provider's wire names carried through or a genuine
reduction. Three verdicts:

- **general** — the concept and every field survive a backend change.
- **general but leaky** — the concept survives; some field carries the provider's own shape through
  to a surface that is not the frame inspector.
- **provider-specific** — one provider's concept renamed.

## Verdict

| Member                            | Produced by                                | Reads                                        | Verdict                     |
| --------------------------------- | ------------------------------------------ | -------------------------------------------- | --------------------------- |
| `TextDelta`                       | `stream_event`, `assistant`/`text`         | `delta.text`, `block.text`                   | general                     |
| `MessageCompleted`                | `_completed`, on message close             | `message.id` (as provenance)                 | general                     |
| `Reasoning`                       | `assistant`/`thinking`                     | `block.thinking`                             | general                     |
| `ToolCallStarted`                 | `assistant`/`tool_use`                     | `id`, `name`, `input`                        | general                     |
| `ToolCallCompleted`               | `user`/`tool_result`                       | `tool_use_id`, `is_error`                    | general                     |
| `ToolCallCompleted.structured`    | same arm                                   | `tool_use_result` (top-level, undocumented)  | **general but leaky**       |
| `TextContent`                     | `_result_content`, string case             | the block's string content                   | general                     |
| `ToolReferences`                  | `_result_content`, list case               | `type == "tool_reference"`, `tool_name`      | **provider-specific**       |
| `OpaqueContent`                   | `_result_content`, default                 | the block list verbatim                      | **general but leaky**       |
| `Outcome`                         | `_result_outcome`, `_activity_outcome`     | `is_error`, `status`                         | general                     |
| `TurnCompleted`                   | `result`                                   | `subtype`                                    | general                     |
| `MessageKey`                      | every arm                                  | nothing — `frame_seq` is the console's       | general                     |
| `Provenance` / `FrameRange`       | every arm                                  | nothing — `frame_seq` is the console's       | general                     |
| `Projection.unprojected`          | `_unprojected`, default branches           | the frame's `type` / `subtype` as map keys   | **general but leaky**       |
| `ProjectionState` / `OpenMessage` | fold state                                 | `message.id`                                 | general                     |
| `Usage`                           | `_usage`                                   | `input_tokens`, `cache_read_input_tokens`, … | general (settled)           |
| `ActivityStarted` / `Completed`   | `system/task_started`, `task_notification` | `task_id`, `tool_use_id`, `description`      | provider-specific (settled) |

## The rule the two verdicts share

The line that separates `ToolReferences` from `ToolCallCompleted.structured` is not "how
Claude-shaped is it" — both are one tool's result shape. It is **where the shape lives in the
type**:

- A per-tool payload behind `Json` is already sanctioned. R6.3 passes a tool's identifier through
  verbatim, `RecordedToolCall.arguments` (<../chat_models.py>) says the same of its arguments
  ("whatever the agent passed, as the protocol carried it"), and `Json`'s own comment in the
  vocabulary names the category: "whatever a provider put in a field this layer passes through
  rather than reads. Open by nature: a tool's structured result is per-tool, not per-protocol." A
  channel that renders a Bash result's `stdout` knows Bash, not Claude.
- A per-tool shape promoted to a **typed member** is not. It puts one tool's field names into the
  vocabulary every backend must produce and every channel may `isinstance` on.

`Json` is therefore the vocabulary's own marker for "not neutral", and it gives the complete leak
surface by type. Three fields carry it: `ToolCallStarted.arguments` values, and
`ToolCallCompleted.structured` (both per-tool, sanctioned), and `OpaqueContent.payload` (per
provider). `Projection.unprojected`'s map keys are a fourth, per-provider surface that the type
does not mark.

## `ToolReferences` is provider-specific

The prime suspect settles against it, and on stronger grounds than expected: this is not one
provider's concept, it is **one tool's result shape on one provider**.

`_result_content` reaches this arm only when a `tool_result` block's `content` is a list whose
every entry has `type == "tool_reference"`; it then keeps each entry's `tool_name` and nothing
else, because a `tool_reference` block carries nothing else.

Three facts place it:

- **The census found 51 of them, and all 51 are the same tool.** Every one sits on a frame whose
  `tool_use_result` holds `{matches, query, total_deferred_tools}` — the deferred-tool search
  (<frame_shape_census.md> § `tool_result`). Not 51 results from many tools that happen to name
  tools; 51 results from one built-in search.
- **The search itself is a Claude Code feature.** <../../cli_protocol/protocol.md> names the
  deferred pool behind `ToolSearch` (§ MCP over the control channel) and does not document the
  block at all. A backend without a deferred-tool pool has nothing to put here.
- **`tool_reference` is not a `tool_result` content block in the Anthropic Messages API either.**
  There, a `tool_result`'s content is a string or `text`/`image` blocks; the API's own tool-search
  returns its own `tool_search_tool_result` block rather than a `tool_result` listing names, and
  `tool_reference` exists in a different position entirely — the pointer inside a
  `tool_addition`/`tool_removal` system block, for mid-conversation tool changes. So this shape is
  the CLI's client-side rendering of its own pool, one level below the API.

The rule that produced the arm has also already been corrected once. The census concluded "every
list is a list of `tool_reference` blocks, 51 of 51"; the later compaction capture found MCP tool
results whose content is a list of `text` blocks, and the projection grew a `text` arm for them.
`ToolReferences` is what remains of that rule.

**A neutral replacement, and what is lost.** Delete the arm and those results fall to
`OpaqueContent`. Nothing leaves the record: `structured` still carries `{matches, query,
total_deferred_tools}`, which is the actual answer, and the block list survives verbatim in
`OpaqueContent.payload`. What is lost is cosmetic and small — the SPA renders
`SessionToolResultView.content`, which `session_views._rendered` currently fills with the
`tool_names` list, so those results would print as the raw block list instead of as a list of
names, on 5.6% of visible tool results.

Keeping a typed "a result that names things rather than carrying prose" arm is defensible, but it
would have to be designed off a second protocol's evidence rather than shrunk from this one. The
nearest general cousin is MCP's `resource_link`, which carries a uri and a description beside the
name; `tool_names: tuple[str, ...]` cannot hold either, so that arm is a different arm, not this
one generalized.

## `ToolCallCompleted.structured` is general but leaky

`_user` reads `frame.payload.get("tool_use_result")` — Claude's own top-level, undocumented field
name, one arm, 1:1, no reduction. That is the `ActivityStarted` pattern exactly. It gets a
different verdict because the **concept** survives a backend change where `ActivityStarted`'s does
not: every tool protocol has output a transcript cannot print, and MCP's `structuredContent` is a
cross-protocol standard rather than Claude's. What does not survive is the value — at least 17
per-tool dict shapes, and the tool set producing them is one harness's.

The plan permits provider-shaped data on the debug surface under three conditions. `structured`
holds one and fails two:

- **Never load-bearing — holds.** No channel reads it. `session_views.SessionToolResultView`
  carries `content` and `is_error` only, so the SPA (<../frontend/x/tool_call.tsx>) never sees it,
  and `room_status.coarse_status` never sees a `ToolCallCompleted` at all.
- **Addressed separately — fails.** It rides inside `ToolResultEntry` in the transcript
  (<../x/conversation_records.py>) and inside `session_events.body` in the record
  (<../x/session_events.py>), so a channel does not have to go and ask a different route for it.
- **Labelled as one backend's wire — fails.** `read_transcript`'s own MCP instructions
  (<../tools/conversations.py>) call it "one vocabulary that names no agent backend", and the
  field's description calls it "the call's structured output, verbatim" — the tool's, not the
  provider's.

**Recommendation: keep the field, fix the labelling, and make the third condition an invariant
rather than a coincidence.** Moving `structured` out of the stream and behind `provenance` would
lose the pairing the vocabulary explicitly argues for (`content` and `structured` are "neither
derivable from the other"), and would make a drilldown mandatory for the thing a reader most often
wants — the exit code, the patch. What is missing is not a different address but a stated
contract: `structured` is opaque to channels, no channel may branch on its shape, and its
descriptions on the MCP surface should say the payload is the tool's own and unnormalized rather
than a shape this vocabulary defines.

`OpaqueContent.payload` is the same finding, narrower: it fires only where no arm matched, and its
docstring already says the branch exists because the block set is the provider's to extend. It
needs the same sentence on the surfaces that serve it.

## `Projection.unprojected` is general but leaky, and mildly

`_unprojected` builds its map keys from the frame's own discriminators —
`f"system/{subtype}"`, `f"{ASSISTANT_FRAME_KIND}/{block_type}"`, or the bare `type` — so the keys
are strings like `tool_progress` and `system/vcs_state_changed`. They reach `TranscriptPage.unreadable`
on the `haku_conversations` MCP surface, via `transcript_entries.unreadable`.

The concept is general: "what the adapter could not read" is something every adapter owes. The keys
are the adapter's. This is the mildest of the three because it is already labelled on both ends —
`Projection.unprojected`'s docstring names the keys as "whatever the adapter calls a frame class",
and `TranscriptPage.unreadable`'s field description calls them "frame classes this release has no
reading for" and points at `read_rollout`. It is a diagnostic count, never rendered as
conversation. No change needed beyond not letting the labelling rot.

## The members that pass, and why

**`TextDelta`.** Two arms produce it and neither carries a Claude name into the event: `_stream_delta`
goes through `frames.text_delta`, which checks Anthropic's streaming discriminators
(`content_block_delta`, `text_delta`) and returns a bare `str`; `_assistant`'s `case "text"` reads
`block["text"]`. The event is `message`, `text`, `provenance`. The docstring already draws the line
correctly — how finely a backend cuts an increment is the adapter's business, and every streaming
backend produces increments.

**`MessageCompleted`.** `text` is the deltas joined; `agent_message_id` is `message.id`, renamed and
explicitly demoted to provenance rather than identity, nullable because the wire sometimes omits it.
Identity is `MessageKey`, which is ours. A backend with no per-message id produces `None` and loses
nothing structural.

**`Reasoning`.** Produced by `case "thinking"` reading `block["thinking"]`, but the field is
`summary: str | None`, and that rename is a real reduction rather than a euphemism: it names what a
backend can actually surface, not what the model did. It also happens to be more durable than
Claude's own wire — on current Anthropic models the raw chain of thought is never returned and what
comes back is a summary or an empty string, which is exactly `str | None`. A second backend that
reports "the agent thought" with no text at all fits without a shape change.

**`ToolCallStarted`.** `id`/`name`/`input` → `call_id`/`tool_name`/`arguments`. `RecordedToolCall`'s
docstring already makes the argument and it holds: every tool protocol worth storing has a name,
some arguments, and an id to answer against. The one shape assumption is that arguments are an
object — `Mapping[str, Json]`, and the arm requires `isinstance(block["input"], dict)` — which MCP,
Anthropic and OpenAI-style function calling all satisfy and a positional-argument protocol would
not. Hypothetical, and not worth pre-generalizing.

**`ToolCallCompleted`, the envelope.** `call_id` from `tool_use_id`, `content`, `structured`,
`outcome`. The envelope is the general half of the pair whose second half is examined above.

**`Outcome`.** Not Claude's result subtypes — the question's premise does not hold. `result.subtype`
never reaches `Outcome`; it reaches `TurnOutcome` in `_result`. `Outcome` is fed from two unrelated
places, `is_error` on a `tool_result` block and `status` on a `task_notification`, by two helpers
that map booleans and two strings onto three members. `UNKNOWN` is what makes it general rather than
merely coincident: it names "the provider reported nothing", which is the common case (`is_error` is
absent on 56% of visible results) and which every backend will need, because the field a provider
would report failure in is routinely absent. Its second producer leaves with `ActivityCompleted`,
which does not change the verdict.

**`TurnCompleted`, beyond usage.** The fields are `outcome: TurnOutcome`, `usage`, `provenance`.
`_result` reads `subtype` alone and nothing else — `is_error` and `stop_reason` are read nowhere,
deliberately, because both are uninformative on this wire, and that deliberate non-reading is the
reduction working. `TurnOutcome` is the console's own enum (<../chat_models.py>), and the fold fills
only two of its three members: `ABORTED` is written by the turn loop from `abort_event`
(<../x/session_runtime.py>), not by any frame. So this field is a console vocabulary partly filled
from the wire and partly from what only the console knows — the opposite shape from a leak. The one
obligation it puts on an adapter is that a turn has a terminating frame to fold; that is an adapter
contract, not a Claude concept.

**`MessageKey`, `Provenance`, `FrameRange`.** `opened_at_frame_seq`, `first_frame_seq` and
`last_frame_seq` are all `frame.frame_seq` — the console's own row number in `session_frames`,
minted by the store, never by the provider. They do bind the vocabulary to the console's
architecture: an adapter can only mint a `MessageKey` if its output is recorded as an ordered frame
log. That holds by construction — the runner envelope is backend-neutral
(<../../runtime/x/bridge/docs/second_backend.md>) and a second backend's frames land in the same
table — so it is a coupling to the console, not to Claude.

**`ProjectionState` / `OpenMessage`.** Fold state, never crossing to a channel. `OpenMessage`
carries `agent_message_id` because the run-grouping rule is "frames sharing one `message.id`", which
is a Claude fact — but it is the adapter's fact, held in state the adapter owns and threaded
through a neutral `ProjectionState`, and an adapter whose messages arrive whole simply never has an
open one.

## Settled elsewhere, restated only so the table is complete

`Usage` passes: `input_tokens`/`output_tokens`/`cached_input_tokens`/`cost_usd`/`duration_ms` is a
reduction to quantities every backend reports, with the aggregation rules that make it one
(counters sum, cost sums with unknown propagating, duration does not sum). It is being deleted for
an unrelated reason — nobody wants the feature — as steps 14–16.

`ActivityStarted`/`ActivityCompleted` fail, on the grounds restated at the top. Steps 11–12.

## Found in passing, not leakage

- **`Authored` is never constructed outside a test.** Every event the fold mints carries a
  `FrameRange`; `session_events.row` and `transcript_entries._provenance` both handle the `Authored`
  case, and no production path reaches either. The union member is right — a console-authored event
  crossed no wire and never will — but it is a shape waiting for the writer that step 4 of the plan
  adds (a rejection recorded as an event), not one anything produces today.
- **`_result_content` can mint the literal string `"None"`.** The `tool_reference` arm builds
  `tuple(str(block.get("tool_name")) for block in blocks)`, so a block matching on `type` but
  carrying no `tool_name` yields `"None"` as a tool name rather than raising or landing in
  `unprojected`. The census shows the key is always present (`{tool_name, type}`, 51 of 51), so this
  has never fired — but it is a strict-data-mapping hole, and it disappears with the arm.

## What was done about it

Both failures are being acted on; neither answer is quite what the audit proposed, and the
differences are the useful part.

- **The activity events are deleted** (#4279), as the audit's premise assumed. The enum members and
  `ck_session_events_kind` stay for a release, because rows of those kinds exist and a member
  removed while its rows survive makes reading one raise rather than degrade.
- **`ToolReferences` is deleted, and so is the union it was an arm of** (#4284). The audit proposed
  dropping the arm and letting those results fall to `OpaqueContent`; what landed goes further —
  `ToolResultContent` is gone entirely and a tool result's `content` is a `str`, the adapter
  rendering it and being the only thing that knows a block shape. The operator's reasoning is that a
  tool result may be lossy, so "provider-specific result rendered as a string" is enough. That
  removes the leak by removing the place a block shape could be named at all, rather than by
  removing one arm and keeping the shape.
- **`structured` stays**, per the rule below: it has readers (`read_transcript` serves it,
  `clip_entry` was written for it), it is not derivable from the string, and it sits behind `Json`.
- **The stored bodies do not change.** `ToolReferencesResultBody` and `OpaqueResultBody` survive
  behind a tombstone, because `session_views._answered` validates every stored row the SPA renders
  and an old transcript must not raise. Only the writer narrows. Their gate is a migration that
  **rewrites** rows to the text shape — not a delete, since unlike the `activity_*` rows these are
  history a surface still shows.

**The rule this audit produced is the part worth keeping**, and it decided the `structured` case
where "how Claude-shaped is it" could not: the line is not how Claude-shaped a thing is, but **where
the shape lives in the type**. A per-tool payload behind `Json` is sanctioned; a per-tool shape
promoted to a typed member is not.
