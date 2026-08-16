# A census of what the CLI actually puts on the wire

Structure only — every prose payload here has been replaced by its shape, length or type. Where an
example helps it is synthetic, written in the observed shape. Read against
<../../cli_protocol/protocol.md>, which this both confirms and contradicts.

Written to give the provider-neutral message format in
<../../plans/chat_runtime_projection.md> § stage 4 fixtures drawn from what occurs rather than from
what anyone remembers occurring.

## Method

Read through the console's own `haku_conversations` MCP tools (`list_conversations`, then
`read_rollout` paged to exhaustion) as the Haku Agent, on 2026-08-16, with the operator's explicit
authorisation to read production transcripts for this purpose.

`list_conversations` caps at 100 and has no cursor, so **the newest 100 sessions is the whole
reachable population** — 2026-08-10 06:51Z through 2026-08-15 07:12Z. Of those:

| Sessions | Frames each | What they are                                        |
| -------- | ----------- | ---------------------------------------------------- |
| 72       | 0           | `failed`, no `surface`; nothing was ever recorded    |
| 14       | 2–5         | handshake only, died before or during the first turn |
| 14       | 62–7,866    | sessions that actually ran turns                     |

Every session with frames has `surface: matrix`; 27 of the 28 are `failed`, one is `ready`.

**15,253 frames** in `read_rollout`'s default view, plus **9,606 `stream_event` frames** asked for by
name — 24,859 total. One session contributes 7,866 of the 15,253, so every count below is given with
the number of distinct sessions it occurred in. A shape seen in one session is an anecdote about that
session; a shape seen in ten is a shape.

The paging is not free: at six concurrent readers the console's Postgres returned
`DeadlockDetectedError` on `read_rollout` repeatedly, and one page also came back
`Unknown tool: 'haku_conversations__read_rollout'` (a reflection-cache miss on a server that was
degraded at connect time). Serial paging was clean. Whoever repeats this should stay serial.

## What this census cannot see

`read_rollout` clips any frame whose JSON exceeds `MAX_FRAME_BYTES` (8,000) and returns
`clipped_bytes` instead of the payload. **703 frames — 4.6% of the default view — are invisible to
this census**, and they are not randomly distributed:

| Kind               | Clipped | Of total    | Note                                                   |
| ------------------ | ------- | ----------- | ------------------------------------------------------ |
| `control_response` | 155     | 155 (100%)  | every `initialize` answer is ~14–16 KB                 |
| `user`             | 270     | 1,302 (21%) | the large tool results — 23 KB is a common size        |
| `system`           | 238     | 11,141 (2%) | includes `init`, which is ~10–12 KB                    |
| `assistant`        | 40      | 1,927 (2%)  | presumably long `thinking` or a large `tool_use` input |

Two consequences worth stating before any number below is trusted:

- **No `control_response` has ever been readable through this tool.** The one frame class that
  proves an interrupt landed is 100% clipped, and `RolloutRecorder` keeps control frames precisely
  so an interrupt that did not take is diagnosable. It is diagnosable from the table and not from
  the tool.
- **The `tool_result` census is a lower bound biased against large results.** 21% of `user` frames
  are too big to read, and size is exactly what distinguishes them. Every "% of tool results" below
  should be read as "% of the tool results small enough to see".

Clipped sizes: 542 frames in the 8–16 KB band, 83 in 16–32 KB, 47 in 32–64 KB, 17 in 64–128 KB,
3 in 128–256 KB, and one over 512 KB.

## Frame types

`read_rollout`'s default view, all 28 sessions with frames:

| `kind`              | Frames | Sessions | In `protocol.md`? |
| ------------------- | ------ | -------- | ----------------- |
| `system`            | 11,141 | 14       | yes               |
| `assistant`         | 1,927  | 14       | yes               |
| `user`              | 1,302  | 14       | yes               |
| `command_lifecycle` | 350    | 13       | yes               |
| `control_response`  | 155    | 28       | yes               |
| `result`            | 129    | 14       | yes               |
| `tool_progress`     | 113    | 7        | **no**            |
| `control_request`   | 54     | 28       | yes               |
| `setup_output`      | 42     | 14       | console-authored  |
| `rate_limit_event`  | 40     | 14       | yes               |
| `stream_event`      | 9,606  | 4        | yes               |

Never observed at all: `active_goal`.

**`system` is 73% of the log and carries almost no information.** 8,512 `thinking_tokens` frames
(56% of everything) and 2,275 `status` frames (15%). Every `status` frame in the corpus carries the
same single lowercase identifier — one distinct value across 2,275 frames — so it is a heartbeat
wearing a discriminator.

**`stream_event` occurs in only 4 of 28 sessions**, all of them on 2026-08-15, and it is heavy where
it occurs: 6,129 / 1,392 / 1,277 / 808 deltas. This answers the measurement stage 1 owes stage 2:
against a session's 7,866 non-delta frames, deltas add 6,129 rows — a ~78% increase in row count on
the sessions that stream, and nothing on the ones that do not.

Delta breakdown (first 400 per session): `content_block_delta` 950, `content_block_start` 181,
`content_block_stop` 178, `message_start` 99, `message_delta` 96, `message_stop` 96. The deltas
themselves are `input_json_delta` 635, `thinking_delta` 168, `text_delta` 87, `signature_delta` 60 —
**tool-call argument streaming is the bulk of it, not prose.**

**`control_request` is `initialize` 54 times over 28 sessions**, never anything else. One long
session performed the handshake repeatedly (four `initialize` exchanges inside its first sixteen
frames) as replicas reattached.

## `system` subtypes

10,903 readable, plus 238 clipped:

| `subtype`                  | Frames | Sessions | In `protocol.md`? |
| -------------------------- | ------ | -------- | ----------------- |
| `thinking_tokens`          | 8,512  | 14       | yes               |
| `status`                   | 2,275  | 14       | **no**            |
| `vcs_state_changed`        | 62     | 7        | **no**            |
| `task_started`             | 25     | 8        | yes               |
| `task_notification`        | 25     | 8        | yes               |
| `background_tasks_changed` | 2      | 1        | **no**            |
| `init`                     | 1      | 1        | yes               |
| `task_updated`             | 1      | 1        | yes               |

Never observed: `commands_changed`, `task_progress`, `post_turn_summary`, `compact_boundary`.

**`init`'s count of 1 is an artefact of clipping, not of the CLI.** Reading each session's first
sixteen frames shows the same prefix everywhere: `control_request`, the clipped `initialize`
response, the prompt, two `command_lifecycle` frames, then a ~12 KB clipped `system` frame in the
exact position `init` occupies. Exactly one session's `init` came in under 8 KB and so is readable.
Every session that ran a turn has one; only one can be read.

`thinking_tokens` is `{estimated_tokens, estimated_tokens_delta, session_id, subtype, type, uuid}`,
and `estimated_tokens_delta` is non-zero on all 8,512.

`vcs_state_changed` carries `{cwd, kind, …}` with `kind` ∈ `commit` (43), `push` (16), `rebase` (2),
`merge` (1).

### `task_started` and its `description`

All 25 are `task_type: local_bash`. **`subagent_type` and `prompt` are absent on every one** —
`protocol.md` lists both as fields the frame carries. No `local_agent` task occurred in this corpus,
so nothing here says what a subagent's `task_started` looks like.

`description` structure, 25 samples:

| Property     | Distribution                                    |
| ------------ | ----------------------------------------------- |
| Length       | 25–49 chars ×11, 50–74 ×11, 150–174 ×1, ≥500 ×2 |
| Lines        | single-line ×22, multiline ×3                   |
| First char   | uppercase ×12, other ×13                        |
| Trailing `…` | never                                           |

So it is **not** a short human label. It is whatever the backgrounded command was, up to and past
500 characters and sometimes spanning lines. Anything that renders it into a status line needs its
own truncation; anything that treats it as a title will paste a multi-line shell command into one.

`task_notification`: `status` is `completed` ×24 and `failed` ×1, `output_file` is always set, and
`summary` is always a string. Note it carries **no `description`** — the field is on `task_started`
only, so pairing them requires the `task_id`.

## `assistant` frames

1,927 frames; 1,887 readable; 40 clipped; 2 are the console's own `partial` reconstructions.

### Every `assistant` frame carries exactly one content block

1,887 frames, 1,887 blocks. There is no such thing on this wire as an assistant frame with mixed
content. The block types are `thinking` 833, `tool_use` 807, `text` 247 — and "in what combinations"
is not a question about frames at all.

**A logical assistant message is a run of frames sharing one `message.id`.** Grouping the 1,887
frames by `message.id` gives **1,253 messages**:

| Frames per message | Messages |
| ------------------ | -------- |
| 1                  | 662      |
| 2                  | 557      |
| 3                  | 30       |
| 4                  | 2        |
| 6                  | 1        |
| 7                  | 1        |

Block combinations **per message**, which is the number a fixture wants:

| Combination                  | Messages | Share |
| ---------------------------- | -------- | ----- |
| `thinking` + `tool_use`      | 446      | 35.6% |
| `thinking` only              | 316      | 25.2% |
| `tool_use` only              | 244      | 19.5% |
| `text` only                  | 105      | 8.4%  |
| `text` + `tool_use`          | 71       | 5.7%  |
| `text` + `thinking`          | 52       | 4.2%  |
| `text`+`thinking`+`tool_use` | 19       | 1.5%  |

**80% of assistant messages contain no `text` block at all.** "Text only" — the shape most
transcript renderers are built around — is 8.4%.

Block order within a message is never arbitrary: `(uniform)` 665, `thinking>tool_use` 433,
`text>tool_use` 70, `thinking>text` 52, `thinking>text>tool_use` 18, and the rest are the same
prefixes followed by additional `tool_use` blocks (up to six in a row).

### The frames of one message are not always contiguous

Of the 634 same-`message.id` frame adjacencies, 621 are adjacent and **13 have a `user` frame
between them**. In all 13 the surrounding blocks are `tool_use .. tool_use`: the model emitted
parallel tool calls, the first result came back, and the _second_ `tool_use` frame of the _same_
message arrived after it.

```text
assistant  id=msg_A  [tool_use t1]
user                 [tool_result t1]
assistant  id=msg_A  [tool_use t2]     <- same message id, after a result
```

A fold that closes an assistant message on the first non-`assistant` frame will split one message
into two, and will attribute `t2` to a message that does not exist.

### Field presence on `assistant`

Three top-level shapes:

| Keys                                                                   | Frames |
| ---------------------------------------------------------------------- | ------ |
| `message,parent_tool_use_id,request_id,session_id,timestamp,type,uuid` | 1,563  |
| … the same plus `tool_use_meta`                                        | 322    |
| `message,type`                                                         | 2      |

The 2-key shape is the console's own `partial` row, not the wire.

`message` keys, all 1,885 wire frames identically:
`content,context_management,diagnostics,id,model,role,stop_details,stop_reason,stop_sequence,type,usage`
— `context_management`, `diagnostics` and `stop_details` are not in `protocol.md`.

- **`message.id` is present on 1,885 of 1,887.** The two absences are both `partial: true` console
  reconstructions whose message is `{content, role}`. So the ~1,417 `session_messages` rows missing
  `agent_message_id` in the production count are **not** the CLI omitting it — the wire supplies it
  essentially always, and the gap is on the console's side of the write.
- **`stop_reason` is `null` on all 1,887.** Nothing in an `assistant` frame says the message is
  finished. The only end-of-message signal is the next frame with a different `message.id`, or the
  `result`.
- `usage` is present on every wire frame, keyed
  `cache_creation,cache_creation_input_tokens,cache_read_input_tokens,inference_geo,input_tokens,output_tokens,service_tier`.
- Every `tool_use` block carries an undocumented `caller`, and it is `{"type": "direct"}` on all 807.
- `tool_use_meta` is a **list**, not an object — always length 1, entries keyed
  `display_name,id,server_display_name` (321) or `display_name,id` (1). It appears only on frames
  whose block is an MCP `tool_use`.
- 9 `tool_use` blocks across 5 sessions have `input: {}`.
- No `text` block is ever the empty string.
- `thinking` blocks are always `{signature, thinking, type}`.

## `tool_result`

910 readable `tool_result` blocks across 14 sessions, all inside inbound `user` frames.

| `content` shape | Blocks | Share |
| --------------- | ------ | ----- |
| bare string     | 859    | 94.4% |
| list of blocks  | 51     | 5.6%  |

**Every list is a list of `tool_reference` blocks** — 51 of 51, keyed `{tool_name, type}`, list
lengths 1 (×20), 3 (×9), 10 (×9), 2 (×7), 5 (×3), and one each of 4, 6, 8. No `text` block, no
`image` block, ever appeared inside a `tool_result`.

That matters more than the 5.6% suggests: a `tool_reference` block **contains no result text**. It
names a tool and nothing else. All 51 are non-error, and all 51 sit on a frame whose top-level
`tool_use_result` holds `{matches, query, total_deferred_tools}` — the deferred-tool search. A fold
that renders `content` and ignores `tool_use_result` renders these as empty.

String result sizes (of the visible ones): 128–256 bytes ×186, 256–512 ×150, 512–1,024 ×143,
1,024–2,048 ×118, 64–128 ×77, 2,048–4,096 ×60, 16–32 ×56, 32–64 ×56, and a tail below 16. Plus the
270 clipped frames, which are the ≥8 KB population this view cannot size.

**`is_error` is absent, not false, on 507 of 910.** The two key shapes are
`content,tool_use_id,type` (507, 13 sessions) and `content,is_error,tool_use_id,type` (403, 14
sessions); of the 403, 68 are `true`. So 7.5% of visible results are errors, and `"is_error" in
block` is not a usable test for anything.

No result was ever an empty string, an empty list, `null`, or missing its `tool_use_id`.

### The structured result is somewhere else

**Every one of the 910 tool-result-bearing `user` frames also carries a top-level `tool_use_result`**
that `protocol.md` does not mention. It is a `dict` 850 times, a bare `str` 59 times and a `list`
once, and its keys are per-tool rather than uniform:

| `tool_use_result` keys                                                        | Frames   |
| ----------------------------------------------------------------------------- | -------- |
| `_meta,content,structuredContent`                                             | 315      |
| `interrupted,isImage,noOutputExpected,stderr,stdout`                          | 289      |
| `matches,query,total_deferred_tools`                                          | 54       |
| `file,type`                                                                   | 41       |
| `gitOperation,interrupted,isImage,noOutputExpected,stderr,stdout`             | 32       |
| `bytes,code,codeText,durationMs,result,url`                                   | 21       |
| `interrupted,isImage,noOutputExpected,returnCodeInterpretation,stderr,stdout` | 14       |
| `content,structuredContent`                                                   | 14       |
| `content,filePath,originalFile,structuredPatch,type,userModified`             | 13       |
| `_meta,content`                                                               | 13       |
| 7 further shapes                                                              | ≤12 each |

This is the channel that carries a tool's _real_ output — exit codes, patches, MCP
`structuredContent`. A provider-neutral message format that models a tool result as
`content: str | list[Block]` will faithfully preserve the prose rendering and silently drop all of
it. There are at least 17 distinct shapes here and it is an open set: it is per-tool, not per-protocol.

## `user` frames

1,302 frames: 1,181 inbound (`from_agent`), 121 outbound (`to_agent`).

**Direction determines the content type absolutely.** Outbound prompts are always
`message.content: str` (121 of 121). Inbound frames are always `message.content: list` (1,032 of
1,032 readable). `message` is always exactly `{content, role}`.

Outbound prompts come in two top-level shapes:

| Keys                                         | Frames | `command_lifecycle`? |
| -------------------------------------------- | ------ | -------------------- |
| `message,parent_tool_use_id,type,uuid`       | 113    | yes                  |
| `message,parent_tool_use_id,session_id,type` | 8      | no                   |

The 8 uuid-less prompts are unobservable and uncancellable, exactly as `protocol.md` describes — and
the console is emitting them today.

Inbound `user` content is `tool_result` 910 times and `text` once. That one `text` frame is the
only `isSynthetic: true` frame in the corpus (`isSynthetic` is undocumented). So: **a `user` frame
from the agent is a tool result 99.9% of the time and prose 0.1% of the time**, and the 0.1% is the
CLI speaking as the user.

## `result` frames

129 frames, 14 sessions. Three key shapes, differing only in `user_message_uuid` +
`request_sent_wall_ms` (109, 13 sessions), neither (15, 2 sessions), and `origin` (5, 1 session).

Uniform across all 129:

| Field                | Value                               |
| -------------------- | ----------------------------------- |
| `subtype`            | `success`                           |
| `is_error`           | `false`                             |
| `stop_reason`        | `end_turn`                          |
| `terminal_reason`    | `completed`                         |
| `permission_denials` | `[]`                                |
| `api_error_status`   | `null` (the only always-null field) |
| `result`             | always a `str`                      |
| `structured_output`  | **key never present**               |

**27 of the 28 sessions are `failed` at the console level and not one `result` frame reports an
error.** Session failure is entirely a console-side concept here (replica loss mid-turn); the CLI's
own turns all completed. So a fold cannot learn "this session went wrong" from `result`.

`num_turns` per result: 1 ×43, 2–3 ×19, 4–7 ×26, 8–15 ×22, 16–31 ×7, 32–63 ×9, 64+ ×3.

`usage` is keyed
`cache_creation,cache_creation_input_tokens,cache_read_input_tokens,inference_geo,input_tokens,iterations,output_tokens,server_tool_use,service_tier,speed`
— `iterations`, `speed` and `server_tool_use` beyond what `protocol.md` implies. `modelUsage` is
present on every frame and is not mentioned there at all.

## `command_lifecycle`

350 frames, 13 sessions, keyed `command_uuid,session_id,state,type,uuid`. 120 distinct
`command_uuid`s, and the per-command state sequences are:

| Sequence                   | Commands |
| -------------------------- | -------- |
| `queued>started>completed` | 110      |
| `started>completed`        | 7        |
| `queued>started`           | 3        |

- **`cancelled` never occurs.**
- 113 of the 120 `command_uuid`s match a prompt the console sent, and the count is exact: 113
  uuid-stamped prompts, 113 matched lifecycles. **The other 7 are commands the console never sent,
  and they begin at `started` with no `queued`.**
- 3 commands were `started` and never completed — the turns whose replica died.

So the triple is not a guaranteed sequence, does not always start at `queued`, and the set of
`command_uuid`s is not a subset of the prompts sent.

## Ordering

Checked over all 28 sessions.

- **14 `assistant` frames follow a `result` with no intervening `user` frame of any kind** — no new
  prompt and no tool result. In all 14 the `message.id` is one not seen before the `result`, so it
  is a new message, not a continuation. The gap contains `system` (and usually `command_lifecycle`)
  frames only. Two of the 14 also have a fresh `initialize` handshake in the gap, which explains
  those; the other 12 do not.
- **No `assistant` frame ever immediately follows a `result`** — there is always at least one
  `system` frame between.
- No two `result` frames are ever adjacent.
- No `tool_use_id` is ever answered twice.
- 302 `tool_result` blocks in 5 sessions reference a `tool_use` id this census never saw. All 5 are
  sessions with clipped `assistant` frames, so this is most likely the clipping and not the CLI —
  but it cannot be ruled out from here, and it is the one anomaly this route cannot settle.
- 199 `tool_use` ids across 14 sessions are never answered before the log ends — again inflated by
  the 270 clipped `user` frames. A clean measurement is not available: the 14 sessions with no
  clipping outside `control_response` are all sessions that made no tool calls at all.

## `tool_progress` — undocumented, and the only nested frame

113 frames, 7 sessions, `type: tool_progress`, keyed
`elapsed_time_seconds,heartbeat,parent_tool_use_id,session_id,tool_name,tool_use_id,type,uuid`.

It is absent from `protocol.md` entirely, and it is **the only frame class in the corpus with a
non-null `parent_tool_use_id`** — 113 present against 2,917 null. No `assistant` or `user` frame is
ever nested, because no subagent ran: every `task_started` is `local_bash`.

The practical consequence: `protocol.md` describes `parent_tool_use_id` as the marker of a subagent
frame, and in production it is currently the marker of a long-running-tool heartbeat. A fold that
routes on `parent_tool_use_id != null` will route heartbeats into a subagent view.

## What will break a naive fold

In rough order of how much damage each does.

1. **A message is a run of frames, not a frame.** One block per `assistant` frame, 591 of 1,253
   messages spanning two or more frames, no `stop_reason` to close them, and 13 messages split
   across an intervening `user` frame. One message per frame is wrong for 47% of messages; closing
   on the next non-`assistant` frame fixes that and is still wrong for the 13 split ones — and both
   are wrong silently.
2. **The tool result you can render is not the tool result.** `content` is a bare string 94% of the
   time, and the 6% that is a list is a list of `tool_reference` blocks carrying no text at all. The
   real output — exit codes, patches, MCP `structuredContent` — is in an undocumented top-level
   `tool_use_result` with at least 17 per-tool shapes.
3. **`is_error` is absent on 56% of results**, `stop_reason` is `null` on 100% of assistant frames,
   and `result.is_error` is `false` on 100% of results including 27 sessions that failed. Every
   obvious "did this go wrong" field is uninformative.
4. **73% of the log is `system` and 15% of it is a constant.** A fold that dispatches per frame will
   spend most of its budget on `thinking_tokens` and a one-valued `status`. Whatever the cursor
   costs per frame gets multiplied by ~11,000 per fourteen real sessions.
5. **Three frame classes and five `system` subtypes are not in `protocol.md`** (`tool_progress`;
   `status`, `vcs_state_changed`, `background_tasks_changed`; plus `isSynthetic`, `tool_use_result`,
   `tool_use_meta`, `caller`, `context_management`, `diagnostics`, `stop_details`, `modelUsage`,
   `origin`). The fold's default case will be reached, routinely, and needs to be a real branch.
6. **`command_lifecycle` is not a clean triple.** No `cancelled` ever, 7 commands starting at
   `started`, 3 never completing, and 7 `command_uuid`s that correspond to no prompt the console
   sent.
7. **`read_rollout` cannot see the frames that matter most.** 100% of `control_response`, 21% of
   `user`, and effectively 100% of `system/init` are clipped at 8 KB. If stage 4 wants fixtures from
   real large results, they have to come from the table, not from this tool.

## Loose ends this route could not settle

- Whether the 302 orphan `tool_result` references are clipping or a real ordering property.
- What a `local_agent` `task_started` looks like, and what a forwarded subagent frame looks like:
  neither occurred.
- `error` results of any kind: `subtype: success` was universal, so the whole error surface of
  `result` is unobserved.
- Anything older than 2026-08-10, since `list_conversations` has no cursor.
