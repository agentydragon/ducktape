# What the chat runtime still asserts that the layer model denies

A survey, not a change. Read against `devel` at `20546bc6a8` (2026-08-17), audited against
`haku/console/plans/conversation_layers.md` on `claude/haku-channel-layers` — its § 1 (what each
layer owns), § 5, § 7 (the settled rulings) and § 11 (the invariants).

What it looks for is narrower than "things that are wrong": **structures inherited from an earlier
design that are now false, and that nothing fails on because the code is internally consistent with
itself.** The model example is `SessionService._frontend_for`, which the plan's § 5 already
dissects — a global plus a guard wearing the name of a mapping, over a property (a session's
channel) that the model says does not exist.

Each finding says what the symbol asserts, which ruling it contradicts, whether anything breaks
today, and whether a § 9 step already condemns it. **"Already condemned" and "nobody has noticed"
need different responses**, so the split matters more than the count: a condemned symbol needs
nothing but patience, while an unnoticed one is what makes the next decision wrong.

Findings are ordered by how likely each is to cause a wrong decision later, not by how ugly it is.

---

## 1. The provenance union is enforced nowhere, and § 11 says it is

**Symbols.** `Provenance = FrameRange | Authored` (<../x/conversation_events.py>:51–59);
`session_events.row` (<../x/session_events.py>:202–207); `ck_session_events_provenance_frames`
(<../database_schema.py>:1228); `ConversationEventKind`'s docstring
(<../chat_models.py>:128–139).

**What it asserts.** That an event derived from a provider's frames names those frames, held as a
union rather than a nullable — § 11's last invariant, described there as "already true and already
enforced, which is why it belongs here: it is the invariant most likely to erode quietly."

**What is actually there.** Two independent statements, and neither holds.

- `ConversationEvent.provenance` is typed `FrameRange | Authored` on **every** member, including
  the ones only a frame fold can produce. So the type says a `MessageCompleted` may legally carry
  `Authored` — no frames, and never will have any. Nothing in the vocabulary can produce a
  console-authored `ConversationEvent`: the console's own facts are `AuthoredBody` /
  `AuthoredEventKind`, a different type entirely. `Authored` on this union is a member with no
  writer and no reader whose only effect is to make the invariant unstatable in the type.
- The table ties `provenance` to the frame columns and to `turn_id`, and ties `call_id` to the two
  tool kinds. It does **not** tie the _kind's category_ to the provenance. A row
  `kind='message_completed', provenance='authored', source_first_frame_seq=NULL` satisfies every
  CHECK. So does `kind='prompt_enqueued', provenance='frame_range'`.

And the writer degrades into exactly that state rather than raising:

```python
frames = event.provenance if isinstance(event.provenance, FrameRange) else None
...
provenance=EventProvenance.AUTHORED if frames is None else EventProvenance.FRAME_RANGE,
```

**Does anything break?** Not today: `claude_code/projection.py` sets `FrameRange` on every event it
mints, so the `Authored` branch is unreachable. It breaks the moment a second adapter takes it — and
it breaks asymmetrically. The write is silent; the read is fatal. `session_views._asked`
(<../x/session_views.py>:260) raises `ValueError` on a tool-call row with no frame, and
`tool_calls()` runs on every `SessionStore.get`, so one such row makes the whole session's transcript
unreadable in the SPA. `reprojection.UnalignableRow` exists for precisely this shape
(<../x/reprojection.py>:86–95) and `check_session` has no caller.

**Ruling.** § 11, "Every event derived from a provider's frames names the frames it came from…
as a union rather than a nullable".

**Scheduled?** No. § 9 has no step for it, because § 11 records it as done.

**Why it ranks first.** It is the one invariant the plan says is safe, on the one seam
(`CliBackend`, a second adapter) the plan says does not exist yet. An adapter author reads
`Provenance` and reasonably concludes that emitting `Authored` from a frame fold is legal — the
type says so, the table accepts it, and the writer converts it without a word.

---

## 2. The Matrix channel is one-per-bot-process in seven more places than § 7 lists, and step 17 is not gated on step 9

**Symbols.** `MatrixSyncService._holding` / `._status_event_id` / `._status_body`
(<../x/channels/matrix/sync.py>:224–226); `MatrixSyncService.pacer`, one `RoomPacer` per service
(sync.py:220) with one collapsing status slot (`RoomPacer._status`,
<../x/channels/matrix/pacer.py>:81); `MatrixSyncService._serviced` (sync.py:419) and `._live_room`
(sync.py:506); `RoomOutboxDrain.drain_once`, which claims only `bound_room()`'s rows
(<../x/channels/matrix/outbox.py>:219–223); `MatrixSessionSupervisor._last_announced`
(<../x/channels/matrix/session.py>:416).

**What they assert.** R3.6a — one bot user services exactly one room — which § 7 withdrew.

**What § 7 says has to move.** Four things: `claim_room`, the supervisor's single binding, the
`MXSE` lock, and `MatrixSessionFrontend` taking no address. All four are real. The list is not
complete, and the omissions are the ones whose failure mode is silent:

| Symbol                                    | With one bot in two rooms                                                                        |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `_serviced` / `_live_room`                | every message from the room that is not the bound one is dropped with a `logger.warning`         |
| `_status_body`                            | room B's status line never appears, because room A's turn already set the string                 |
| `_status_event_id`                        | room B's edit relates to an event id in room A; the homeserver takes it and no client renders it |
| `_holding`                                | one boolean for all rooms — a hold announced in A suppresses the notice B owes                   |
| `RoomPacer._status`                       | two rooms' status changes collapse into one slot; the first is overwritten before it is sent     |
| `RoomOutboxDrain`                         | replies for every room but the bound one sit unsent forever, with no error                       |
| `MatrixSessionSupervisor._last_announced` | one string, so a lifecycle transition in A suppresses the identical one in B                     |

**Does anything break?** Nothing today — `claim_room` refuses the second room, so the second room
does not exist. Every row above is latent and becomes live the day step 17 lands.

**Scheduled?** Yes, all of them — § 11's "what disappears" retires the per-process latches, the
pacer-as-deque and `RoomOutboxDrain`, and § 9 puts that at **step 9**. But § 9's dependency line
reads "17 on 2's reader half and on idle sessions." **Step 17 is not gated on step 9.** So the
order the plan permits includes a release in which one bot holds several rooms while all of the
above is still one-per-process, and none of it raises.

**Why it ranks second.** This is a scheduling decision the plan will make, and it will make it from
a list of four. The list is eleven, and the seven it omits fail quietly.

---

## 3. An abort notice has no conversation-layer home: it is prose inside a frame-pointed message, or it is lost

**Symbols.** `_run_turn`'s `final_text += f"\n\n{ABORTED_NOTICE}"`
(<../x/session_runtime.py>:637–638); the three-way branch at 639–658; `_speak`'s
`if frontend is None: return` (session_runtime.py:711).

**What it asserts.** That an abort is something a _channel_ is told, so a session with no channel
needs no record of it.

**What is actually there.** Two representations, both wrong in a different way.

- In the two branches that have a message row (639, 645), the notice is **concatenated into
  `session_messages.content`** — a row whose `source_first_frame_seq` is required to be set
  (`ck_session_messages_assistant_pointed`) and now spans prose no frame in that range carries. The
  stored `MessageCompleted` event and the message row disagree about what the agent said, and
  nothing compares them: `reprojection.check_session` aligns `session_events` against the fold and
  never looks at `session_messages`.
- In the third branch — the agent completed a message and was aborted before opening another —
  `carried_final` is False and `queued_reply` is False for any session with no room, so `spoke` is
  False, `_speak` runs, and `_speak` returns immediately on `frontend is None`. **For an SPA
  session the abort notice is written nowhere at all.** `test_an_aborted_turn_says_so_on_its_own`
  (<../x/test_session_runtime.py>:1072) pins the Matrix half; there is no SPA counterpart, because
  under the current structure there is nothing to assert.

**Ruling.** § 7, "An abort is an event… the room's 'this was aborted' notice is a projection of that
row rather than its record." § 11, "Nothing is announced that is not recorded."

**Scheduled?** Half. § 7 names the abort notice's durable copy in `session_outbox` keyed by
`turn_id` and step 6 moves it. It does **not** name the copy inside `session_messages.content`, and
it does not name the branch where the SPA gets neither.

**Why it matters beyond the notice.** The same three branches decide where `result.result` lands.
The rule "a channel that holds no copy needs nothing here" is what produces the loss, and it is the
frontend-per-surface structure (§ 5) restated as a data-loss path rather than as a name.

---

## 4. `surface` is a single value on the read API and in the SPA, so a conversation has exactly one channel

**Symbols.** `ConversationSessionSummary.surface` / `.room_id` (<../x/session_views.py>:105–106);
`ConversationSessionView.surface` / `.room_id` (session_views.py:145–146); `Conversation.surface`
(<../x/conversation_records.py>:33); `surfaceLabel` in
<../frontend/x/conversations_page.tsx>:44–46, rendered as the list row's title.

**What it asserts.** That "which channel is this conversation on" has one answer, and that a
conversation is either a Matrix room or a browser tab.

**What the model says.** § 11: a conversation has attachments, plural and concurrent — "a room, a
browser, whatever comes next". § 7's acceptance behaviour 5 is exactly the case this shape cannot
render: one conversation, two surfaces, both open.

**Does anything break?** Not yet — but the SPA already sends prompts into Matrix-surfaced sessions
(finding 6), so a conversation labelled "Matrix" is already one the operator is talking to from a
tab.

**Scheduled?** The _column_ is: § 11's table retires `sessions.surface` and `sessions.room_id` in
favour of the attachment. The **API field and its renderer are not named anywhere**, and § 9 step 3
rewrites the conversation list surface. That is the step that will decide whether the new list
carries `surface` forward — and today's field is the obvious thing to carry.

---

## 5. The turn loop reads the provider's `result` envelope, and `x/README.md` says the live path does not

**Symbols.** `_run_turn` reading `result.get("subtype")`, `result.get("stop_reason")` and
`result.get("result")` off the raw payload (<../x/session_runtime.py>:630, 636); `_CompletedTurn.frame`
(session_runtime.py:174–182); the claim in <../x/README.md>:225 — "**All four interpreters are
gone.** `_run_turn` projects each frame as it lands and acts on the events, so the live path no
longer knows that `assistant`, `stream_event` and `result` exist."

**What is actually there.** The code is honest about it — `_CompletedTurn.frame`'s comment calls the
read "the design's own escape hatch… the seam working rather than leaking" — and it is doing real
work the neutral vocabulary does not carry (a failure's reason; the prose of a turn that spoke
nowhere else). The README's claim is the part that is false, and it is the sentence a reader
reaches for when asking whether the runtime is backend-neutral.

**What a second backend hits.** Neither key exists on another harness's terminal frame, so
`final_text` silently becomes `""` when a turn's text arrived only there, and a failed turn raises
`the agent's turn failed: None: unknown error`. Both degrade rather than fail loudly.

**Ruling.** § 11's neutrality invariant is stated about **channels** ("No channel knows a provider's
frame shape"). The runtime layer has no such invariant, and this is the leak that is actually there.
§ 11's own "what enforces the rule does not exist yet" paragraph names only
`x/frame_projection.py`'s direct import of `claude_code/projection`.

**Scheduled?** No. Deciding whether `TurnCompleted` should carry a failure reason is
architecture-level and belongs to the operator, not to this audit. What is reportable is that the
README asserts a property the code does not have.

---

## 6. A prompt typed in the browser into a Matrix session reaches the room only as an answer

**Symbols.** `ConversationComposer` (<../frontend/x/conversation_composer.tsx>), wired into the
conversation detail page (<../frontend/x/conversations_page.tsx>:385); `enqueue_prompt`, which reads
no `surface` (<../x/session_store.py>:506).

**What changed.** The composer landed recently and is documented in <../x/README.md> § "Prompting a
session the browser did not create". It is correct and it is the plan's step 1 in spirit. But
nothing projects a `PROMPT_ENQUEUED` event into a room: the room's copy of the operator's half
exists only because it was typed there.

**Consequence.** An operator prompting a Matrix conversation from a tab makes the room show Haku
answering a question the room never saw. § 11's acceptance behaviour 4 says the first case "does not
work at all today" — that was true before the composer as a thing nobody could reach, and is now
true as a thing one click produces.

**Scheduled?** Yes: § 4's "The operator's own prompts are part of what a surface shows", landing
with step 9. Nothing is wrong with the composer; what is worth recording is that the two halves
landed in the order that makes the gap visible.

---

## 7. `Conversation` names a session, and it can end

**Symbols.** `conversation_records.Conversation` (<../x/conversation_records.py>:31–37), the
`haku_conversations` MCP record, whose primary key is `session_id`;
`ConversationSessionSummary`/`ConversationSessionView` (<../x/session_views.py>:99, 141);
`GET /api/conversations/{session_id}` and `…/frames` (<../x/session_runtime.py>:772–797);
`SessionStore.list_operator_conversations` (<../x/session_store.py>:282) grouping message counts
per session; the SPA's status badge on a list row
(<../frontend/x/conversations_page.tsx>:222 and 352).

**What it asserts.** That a conversation is a session — so it has one surface, one message count,
one `created_at`, and a terminal status.

**What the model says.** § 1: a conversation is identity spanning sessions. § 7: "A conversation
never ends. No `ended_at`, no terminal state." Today the operator's conversation list shows rows
badged `closed` and `failed`, which are session lifecycle facts wearing the conversation's name.

**Scheduled?** Partly. § 9 step 3 rewrites the list surface and merges `/chat` with
`/conversations`; § 6 introduces the identity. Neither names the **record shapes** — the MCP
`Conversation` model, the two `ConversationSession*` views, the route's `{session_id}` path
parameter — and those are the API surface a second reader is written against.

**Rank note.** This is mostly a naming fault over a structure that is already condemned, which is
why it is here and not higher. It earns its place because the naming is what will make step 3's
rewrite look like a rename.

---

## 8. Who reads `sessions.room_id`

§ 9 step 2 says it "subsumes `matrix_conversation` and `sessions.room_id`", and § 11 names
`RoomTranscript.recent`'s join as the concrete casualty. The plan never enumerates the rest. This is
the list, so the step can be scoped rather than discovered:

| Reader                                                                                  | What it does with it                               |
| --------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `_enqueue_reply` (<../x/session_store.py>:1447, 1452)                                   | branches on it and writes `session_outbox.room_id` |
| `SessionStore.room_of` (session_store.py:1211)                                          | exists only for `_frontend_for`                    |
| `list_operator_conversations` / `get_operator_conversation` (session_store.py:303, 332) | puts it on the SPA's read models                   |
| `list_conversations` (session_store.py:852)                                             | puts it on the MCP `Conversation` record           |
| `RoomTranscript.recent` (<../x/channels/matrix/session.py>:194)                         | joins on it to read a room's tail across sessions  |
| `PostgresIndexSearcher._chat` (<../recall_index_reader.py>:152–164)                     | attaches it to every `haku_index` conversation hit |

Only the last two are inside a channel or documented; `recall_index_reader` is the one nothing has
noticed — an unrelated subsystem outside `x/` reading a Matrix address off `sessions` and publishing
it on an MCP result. § 11's structural test ("`session_store` contains no reference to `room_id`")
would catch three of these and none of the other three.

Also on this seam: `_as_prompt` (<../x/channels/matrix/session.py>:289) renders Matrix event ids
into the prompt text. § 11 names the parse (`Parsing [event_id] out of prompt text`); what it does
not name is where that text then travels — into `RoomTranscript.recent`, into the replacement
session's system prompt, and into the `haku_index` chat corpus, where the ids are embedded.

---

## 9. `_frontend_for`'s doctrine has a test and a README sentence

The symbol itself is § 5's worked example and needs nothing said about it. What the plan does not
record is that the concept is written down in three more places, each of which reads as a
specification:

- `ChatFrontend`'s docstring (<../x/session_runtime.py>:147–165): "The chat channel a session is
  attached to… a channel serves one room and a session serves one channel… **The service picks this
  by reading the session's `surface`**." (It reads `room_of`, not `surface`; under
  `ck_sessions_matrix_room` the two agree.)
- `test_only_the_sessions_that_serve_a_room_are_attached_to_the_frontend`
  (<../x/test_session_runtime.py>:787), whose docstring states the doctrine as intent: "One console
  serves both surfaces, and the frontend is bound to its room — so which sessions it serves is the
  session's own record."
- <../x/README.md>:645: "The composition in `app.py` is what ties a frontend to the sessions it
  serves."

§ 5's instruction is right — leave the symbol alone, it is deleted at step 9 rather than improved.
The point of listing these is that step 9 deletes a **green test** and two prose statements, and
anyone who reads the test before the plan will read it as the spec.

---

## 10. `x/system_prompt.py` requires a room id

`SessionIntroduction.room_id: str` (<../x/system_prompt.py>:52) is required, not optional, in a
module at the runtime level whose only consumer is `MatrixSurface`. § 5 sanctions `system_prompt` as
"the one use that is honestly surface-dependent", so the port method is right; what is off is that
the neutral module's type makes a room mandatory, so no non-Matrix session can have an identity
prompt at all — `_appended_prompt` returns `None` whenever there is no frontend
(<../x/session_runtime.py>:287). `HistoryMessage.sender` is likewise an MXID in a runtime-level type.
Low stakes, unscheduled, and cheap whenever the port is rebound per attachment.

---

## 11. The frame inspector does not say whose wire it is

§ 11 sanctions the debug frame surface under three conditions: addressed separately, never
load-bearing, and **"labelled as one backend's wire", not as the conversation**. The first two hold
(`/api/conversations/{id}/frames` is its own route; nothing renders off it). The third does not: the
page's subtitle is "The agent protocol as it crossed the wire"
(<../frontend/x/session_frames_page.tsx>:120), and `frame_log.frameSummary`
(<../frontend/x/frame_log.ts>:80–97) switches on Claude's `type` values, content blocks and
`tool_use_id` with no statement that these are Claude Code's. One stale line beside it: "No stream
deltas recorded — only the SPA chat surface streams them" (session_frames_page.tsx:155) —
`--include-partial-messages` is unconditional (<../../runtime/x/bridge/options.py>:82), so every
session records deltas.

---

## Candidates rejected, and why

Each of these looks like a layer violation and is not. They are listed because a later pass will
find them again.

- **`session_changed` names a session, not a conversation** (<../console_events.py>,
  <../x/session_live_updates.py>). § 8 lists this under "What is already the right shape" — the wake
  carries no payload and the subscriber reads. Not a finding.
- **`EventTag` carries `session_id`** (<../x/channels/matrix/client.py>:109). § 3 names the tag as
  the correspondence key and lists exactly these fields. Endorsed.
- **`matrix_access_token` and `matrix_sync_watermark` are keyed by bot user**
  (<../database_schema.py>:1475, 1489). Correct: the credential is per bot and a `/sync` token is
  per user across all rooms. Only `matrix_held_batch` is wrongly per-user, and it is deleted at
  step 4 — before step 17 could expose it.
- **A tool's identifier is passed through verbatim** into a status line
  (`room_status.coarse_status`) and stored on `ToolCallBody`. R6.3 sanctions it explicitly, and the
  vocabulary audit's rule ("the line is not how Claude-shaped a thing is, but where the shape lives
  in the type") puts `ToolCallCompleted.structured` behind `Json` on the sanctioned side.
- **`channels/matrix/testing/console_deployment.py` imports `ASSISTANT_FRAME_KIND`.** § 11 names
  this as the one exception worth writing down. Present, single, and in a test helper.
- **The frame inspector reads Claude's content blocks.** The carve-out covers it; only the labelling
  condition fails (finding 11).
- **Matrix cannot start or abort a session.** <../plans/session_channels.md> settles that parity is
  not symmetry: the supervisor owns provisioning by R3.1/R3.2. A feature gap, not a layering claim.
- **`_CompletedTurn.frame` existing at all.** Appealing an event to its frame is the design's own
  escape hatch. What finding 5 reports is the README's claim about it, not the field.
- **`session_outbox`'s docstring calling itself neutral** (<../database_schema.py>:1332). § 5
  already names this as the sentence to correct when the table moves. Condemned and noticed.
