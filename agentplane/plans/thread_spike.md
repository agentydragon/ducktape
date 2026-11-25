# Thread spike

Scope for an additive, off-by-default spike of the Thread: the messaging object a person and agents
act on ([naming ADR](../docs/adr_journal_and_thread.md)). Nothing here is released; the operator
decides when to build it. The task DAG's
[`AGENT_AUTHORED_CONVERSATIONS_DESIGN`](task_dag.md#agent_authored_conversations_design--conversations-agents-act-on-through-tools)
records the direction (operator, 2026-11-17 13:21 UTC), and the
[CLI experiment](../debug/authored_conversation/README.md) is the evidence it builds on, and
[prior art](thread_prior_art.md) collects how other agent systems do it.

**Decided (operator, 2026-11-17 13:29 UTC):** "i agree, i think Notifications would probably make
sense as a natural home at least initially". Threads live in the Notification service.

## Hypothesis and test

An agent working in an Agentplane Session can use a Thread as its main channel with the operator:
it reads the Thread through a tool, publishes complete messages to it, and handles edits to messages
it already read. If that holds on Codex and Claude, History can become the diagnostic log.

The operator runs real work through Threads for a while. The comparison against working through
History is: replies the agent should have published but did not, edits it missed or misapplied, and
whether the operator still needed the History view to follow the work. Focused CLI probes with an explanatory prompt elicited replies after reset and correction
on Qwen and Luna. Exercise those situations with the hosted harnesses.

The operator reiterated the two-view goal on Nov 22. The messenger pane shows authored posts and
their revisions. History gives a diagnostic account of model activity, tool calls and captured
context, with enough detail to explain a missing post. The [CLI Thread and Trace views](../x/authored_conversation/README.md)
already demonstrate this distinction. For the hosted trial, display the same Session in both
panes: send a request, let a tool run, edit a message and follow the agent’s published reply.
Check that tool traffic stays in History, that both participants see the same message revisions,
and that a missing reply can be investigated from the recorded activity.

## What exists

- [Session-keyed inboxes](../notification_service/README.md), with entries from Actions, GitHub and
  workload messages (`EventIdentity` in
  [`notifications.proto`](../notification_service/api/notifications/notifications.proto)).
- Notices: the worker submits `Agentplane inbox notice: {...}` to the Session through the Dispatcher,
  batched and woken as for any inbox entry; the agent reads and acknowledges with the CLI.
- `agentplane notifications send` delivers a plain-text message to another Session's inbox. Its
  `from_session` is a caller label that nothing verifies.
- The App's browser facade reaches Notifications at `/upstream/notifications/`, GET only, with the
  App's service credential after operator login.
- The [CLI experiment](../x/authored_conversation/README.md): `read_thread`, `read_message`,
  `post_message`, `edit_message` and `yield_turn` tools, immutable revisions, operator edits.

## Spike scope

**Store, in its own Notifications package** (`internal/thread`, own migrations), so it can move
out later:

- `Thread`: ID, creator, creation time.
- `Participant`: a Session ID, or the operator. A Thread has one or more Sessions; the spike's UI
  shows one.
- `Message`: ID, Thread, position, author participant, current revision.
- `Revision`: immutable body (Markdown, the existing message size limit) and author. Edits append a
  revision; there is no delete.

**API**, added to the `Notifications` service with gRPC-Gateway annotations: `CreateThread`,
`AddParticipant`, `PostMessage`, `EditMessage` (expected current revision, so a stale edit fails),
`ReadThread` (after a position, with revisions), `ListThreads` (by participant) and `WatchThread`
for the browser. Writes take idempotency keys like `SendMessage`.

**Authorization:** a workload may act as a Session participant only while it runs as that Session's
recorded execution Identity, as inbox ownership already checks. Sessions sharing one Identity can
therefore post as each other; the spike accepts that. The operator participant is the App's service
caller after operator login, the path the App's read facade already uses. Cross-Identity
participants are out of scope.

**Delivery:** each post or edit appends a Thread entry (Thread, message, revision) to every other
Session participant's inbox, through the existing notice batching and wake. The agent reads the
Thread with the CLI; the body is never pushed. The new `EventIdentity` variant ships in the CLI and
App readers one release before Notifications writes it.

**Session opt-in (operator preference, Nov 22).** Offer an optional authored Thread attachment
when starting a Session, for example by selecting it in the Session creation request. Record the
participant and Thread ID so a reconnect or retry reopens the same conversation. The exact flag,
whether to create or select a Thread, and which service records the attachment are design choices.
The app can gate exposure of the experiment across a deployment. Each Session's attachment
controls its messenger pane, tool discovery and guidance. A Session may run with zero Threads.

**Agent surface.** Compare existing Agentplane tools (CLI, Action or MCP as appropriate for the
harness) with a small set of central sampler tools for reading, posting and editing. Choose one
entry point for the first participating harness. It must act as the recorded participant and
respect the same Thread and revision checks as the UI. Provide the explanatory guidance from the
CLI experiment only to opted-in Sessions. A hosted CLI and central sampler rollout can be tried
independently.

**App.** For a Session with an attachment, open the messenger pane on that Thread with a composer,
in-place edit and revision history. Keep History accessible. The existing optional
`frontend_config.threads` proposal can hide the experimental UI across a deployment.

## Rollout

Land the Notifications store, API and authorization, then the reader for the new inbox entry before
enabling its writer. Add the per-Session attachment and the chosen tool surface, then expose the pane
to the operator. Check two Sessions in the same deployment: one starts with a Thread and can post
and edit after reconnect, while one starts with defaults and uses today's conversation view without
creating a Thread or advertising its tools. Record messages and trace events through both paths.
Disabling new opt-ins preserves existing posts and revisions.

The operator wants to try the messenger on real work before deciding on the default UI. Keep the
History fold, projections and sync during the trial. A later simplification must account for raw
diagnostic reads, command reconciliation and retained data.

## Proposals from prior art

Not decided; each would change the scope above if adopted. From the
[research lane's findings](thread_prior_art.md#agent-messaging-elsewhere):

1. **Explicit silence.** A `no_reply` command beside `post`. A turn that ends with assistant text but
   neither a post nor `no_reply` counts as a missed publication: the agent gets one nudge, and the
   text is never posted for it. What notices such a turn is open: Notifications sees only Thread
   writes, so it takes a History read of the turn.

   **For central Sessions, the operator (2026-11-17 13:37 UTC):** "at least in central sampler case
   we could easily call in forced-tool-call mode and loop until the agent calls the tool." The
   sampler owns the turn loop, so it can decline to end a turn: on `end_turn` with no post and no
   `no_reply` in the turn, append a nudge and sample again. Two limits from the code
   (`sampler/providers`): neither provider sends `tool_choice` today, and each request's
   tools are code mode's `exec` (plus `wait` on Responses), so a post would run inside an `exec`
   cell and forcing a tool call would force `exec`, not a post. Staging's Anthropic models also run
   with adaptive thinking, where a forced `tool_choice` is reported rejected
   ([prior art](thread_prior_art.md#failure-modes); not tried here). The loop is therefore
   "re-sample until a post or `no_reply` appears", checked by the sampler on the Actions the turn
   ran, not `tool_choice=required`.

2. **Read cursors.** Each participant has a read position that `read` advances. An inbox entry then
   means "new since your cursor", one per Thread and Session, collected under the per-Session notice
   interval proposed for [persistent agents](task_dag.md#scheduled_agent_wake_design--choose-the-durable-scheduled-wake-contract)
   and summarized on overflow, instead of one entry per post or edit.

3. **Progress as status.** A participant status the pane shows apart from messages, so "working on
   it" is never a message. The experiment's guidance to edit one progress message stays the fallback.

**Gotcha for the CLI experiment:** Claude rejects a forced `tool_choice` while extended thinking is
on, so the experiment's `yield_tool` turn control, which requires a tool call, does not carry over to
Claude with thinking. Hosted Sessions are unaffected: the spike does not set `tool_choice`.

## Observation: coordinating across Threads

Not part of the spike and not a decision. The operator, 2026-11-17 13:33 UTC, on this project's own
coordinator chat: "i do like that it looks like you-the-coordinator get notified on whatever happens
in any thread, though - the actual user-visible agent messages and the user responses are likely only
like <5% of any given session's context and they are the part that needs coordination with the
user". He had not planned "an explicit coordinator-worker style of separation of concerns". A later
use of Threads: a Session that participates in, or is notified about, many Threads can coordinate
across them while carrying only their messages, not the working context of the Sessions in them.

## Out of scope

History or Journal changes, Electric, central sampler Sessions, several Sessions in one Thread in the
UI, cross-Identity participants, uploads, streaming partial messages, and message deletion.

## Open questions

1. **Does the operator's first message also go to the harness as ordinary input?** Default: no.
   Only the one "Open Thread" input does; later messages arrive as inbox notices, which is what the
   spike tests.
2. **Does an edit notify?** Default: yes, as a new entry naming the revision, since handling edits
   is part of the test.
3. **Is the operator one participant, or each signed-in person?** Default: one operator participant;
   Agentplane has one operator today.
