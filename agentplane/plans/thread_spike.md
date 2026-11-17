# Thread spike

Scope for an additive, off-by-default spike of the Thread: the messaging object a person and agents
act on ([naming ADR](../docs/adr_journal_and_thread.md)). Nothing here is released; the operator
decides when to build it. The task DAG's
[`AGENT_AUTHORED_CONVERSATIONS_DESIGN`](task_dag.md#agent_authored_conversations_design--conversations-agents-act-on-through-tools)
records the direction (operator, 2026-11-17 13:21 UTC), and the
[CLI experiment](authored_conversation_experiment.md) is the evidence it builds on.

**Decided (operator, 2026-11-17 13:29 UTC):** "i agree, i think Notifications would probably make
sense as a natural home at least initially". Threads live in the Notification service.

## Hypothesis and test

An agent working in an Agentplane Session can use a Thread as its main channel with the operator:
it reads the Thread through a tool, publishes complete messages to it, and handles edits to messages
it already read. If that holds on Codex and Claude, History can become the diagnostic log.

The operator runs real work through Threads for a while. The comparison against working through
History is: replies the agent should have published but did not, edits it missed or misapplied, and
whether the operator still needed the History view to follow the work. The CLI experiment's
explanatory prompt fixed missed publications on Qwen and Luna; this tests it on the hosted
harnesses.

## What exists

- Session inboxes keyed by Session (`INBOX_SESSION_KEYING`), with entries from Actions, GitHub and
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

**Agent surface:** `agentplane threads read | post | edit | list` in the agent CLI, which hosted
Codex and Claude Sessions already run, so no harness changes. A bundled guide (`agentplane threads
guide`) carries the experiment's explanatory prompt: the operator reads only what is posted to the
Thread, so publish complete replies there; ordinary assistant text goes only to History. Central
sampler Sessions would need an Action and come later.

**App, behind an optional `frontend_config.threads` section** (absent means off):

- an "Open Thread" action on a Session page, which creates a Thread with the operator and that
  Session and submits one input naming the Thread and the guide;
- a Threads pane that follows `WatchThread`, with a composer, in-place edit and revision history;
- the App's proxy allows the Thread write routes to Notifications, as a plain proxy.

## Rollout

Separate PRs: (1) store, API and authorization in Notifications; (2) CLI commands, guide and the
reader for the new inbox entry; (3) Notifications writes inbox entries; (4) App setting, proxy routes
and pane; (5) the setting on staging. Each is additive; reverting (4) or removing the setting hides the
spike. Dropping it later deletes the package and, in its own PR, the tables.

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
