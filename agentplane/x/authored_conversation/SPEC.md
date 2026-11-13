# Authored conversation CLI contract

The experiment owns one human/agent conversation per state directory. It samples through a
configured OpenAI-compatible Responses endpoint with streaming and SDK retries disabled. There is
no native harness and no dependency on Agentplane's Session, History or notification services.

## State and presentation

- `thread.sqlite` retains messages with stable UUIDs, original ordering and CLI-assigned authors.
  Bodies are immutable revisions with a monotonically increasing change sequence and UTC recording
  time. The thread displays the latest revision; past revisions remain readable.
  Explicit `post_message` and `edit_message` calls publish assistant messages in authored mode. Model text,
  reasoning and tool results remain diagnostic; they are never promoted into conversation text.
- Terminal input/output opens a full-screen Textual app. Its Thread view renders the current
  conversation on startup, updates edited messages in their original positions, and appends new
  messages. Bodies render as Markdown with author/ID/revision headers. Background publication
  preserves the active composer and edit draft. Stored bodies and editors retain Markdown source.
- The separate Trace view lists diagnostic events and shows the selected event's complete JSON.
  It reads newly appended events incrementally while open, follows the latest event when selected,
  and preserves selection when the user is inspecting an older event. Diagnostic content does not
  enter the Thread view. Active runs show a spinner, elapsed time and model-call number.
- Redirected input/output uses the line-oriented CLI. Messages, `/thread` and `/revisions` use Rich
  Markdown. `/trace` emits raw JSONL. Redirected output uses no terminal styles under normal color
  settings.
- Working context is in memory. Resetting it preserves the thread and trace. Authored mode starts
  again with instructions and notices, then reads the thread through tools. Chat mode can rebuild
  context from visible conversation messages. Neither mode restores or automatically replays work.
- `trace.jsonl` records complete provider responses, notices, calls/results, publication, reset and
  run outcomes, correlated by session/run IDs and sampling step. Credential values and HTTP headers
  are not recorded. `/trace` is an explicit diagnostic view.

## Input and tools

Human input commits before a run starts. Authored-mode notices identify human posts and edits by
message ID and revision without copying their bodies into context. Changes to old messages wake an
idle agent too. Input arriving while a sample or tool is active is announced at
the next sampling boundary, including before stopping after a final response. A notice does not
claim that a message was read or addressed. One loop owns context at a time; reset requires idle.
Terminal input setup preserves stdout/stderr flags, including when all three streams share a TTY,
so large diagnostic events remain printable.

`read_thread` returns ordered, bounded pages of current revisions and an exclusive message-position
cursor. Edits do not move messages or change that cursor; notices identify which old messages need
rereading. `read_message` retrieves the current revision by ID, or a specific past revision.
`post_message`
commits an agent message before displaying it and returns its ID; publication does not wake the
agent. `edit_message` replaces an agent message's visible body while keeping its identity and
position; it returns the committed revision before displaying it. Both participants may edit only
their own messages and must supply the revision they read. Stale edits fail without changing state;
concurrent writers cannot silently overwrite. Empty or oversized replacements fail as posts do.
The model cannot choose a message's author. Chat mode exposes no conversation tools.

The prompt guides the agent to edit recent messages for corrections or clarifications, and post new
messages for additions or to advance the conversation. For messages far back in the conversation,
it should post a new correction with enough context. This is conversational guidance, with no
enforced recency cutoff.

Edits are new information. Earlier tool results, model context and trace entries remain unchanged;
editing never undoes or replays commands. An agent edit counts as a publication. An edit-only notice
may be acknowledged by yielding without publishing; it does not erase an unanswered new post.

In the full-screen app, Enter inserts a newline and Ctrl+S sends a new message. Edit opens a separate
multiline source editor; Ctrl+S saves and Esc cancels, leaving the new-message composer intact. The
version check uses the revision captured when the editor opened; a conflict leaves the edit draft
open without overwriting newer state. Revisions opens past versions of either participant's message.
F2/F3 switch Thread/Trace, F5 continues work, Ctrl+R resets idle context and Ctrl+Q cancels work
and exits. Workers belong to the app and are cancelled on exit. Piped input supports explicit-version
edits, EOF drains active work, and `/quit` cancels it.

Opening a legacy state directory migrates it transactionally, retaining message IDs, authors,
positions and exact bodies as revision 1. Legacy recording timestamps denote migration time.
The CLI rejects a database from a newer schema version. One CLI owns a state directory; stop the
old process before upgrading. Older binaries cannot open the upgraded schema; use a pre-upgrade
copy if rolling back. Never replace the upgraded database with a copy that discards newer messages.

`read_file` reads within the resolved workspace, reports truncation, and refuses symlink escapes.
`run_command` starts a shell in that workspace, bounds time/output, and reaps its process group
on completion, cancellation or a bound. Commands retain ordinary user privileges. Bounds and
errors do not imply that a command had no effects.

## Completion and limits

Only completed provider responses and completed function calls can execute tools. Native reasoning
and call/result items are retained in working context across requests. With default implicit turn
control, a response without calls ends a run only after newly arrived human input has been considered
for announcement. A missing
published reply after the latest announced new post is reported as `missing_publication`; the CLI
does not repair it with reminders or publish the model's private text.
`completed` reports loop completion with any required publication, not whether that publication answered the
question well. The experiment's conversation review assesses that semantic condition.

By default, `turn_control=implicit` uses automatic tool choice. The authored-mode
`turn_control=yield_tool` variant requests `tool_choice=required` on every sample, including after
publication, and exposes `yield_turn({})` to end work and wait for human input. A valid yield
records a tool result; if human input arrived while it was in flight, announce that input and
continue instead of stopping. Yielding does not publish a message. A plain response without calls
is an incomplete protocol response. A yield must be the last call in its batch; work after a yield
is rejected before executing effects. Earlier calls, including publication, execute in order.
Invalid yield arguments return a tool error without stopping. Work-tool loops
still hit the step limit, and a yield without a needed publication reports `missing_publication`.
Chat mode rejects explicit-yield control. The two protocols are separate comparisons.

Step limits and incomplete provider responses produce explicit incomplete outcomes. API failures
are recorded by category and surfaced to the operator. The loop does not retry failed requests or
automatically replay previous tool calls. EOF drains active work; `/quit` cancels it. Reopening a
state directory requires new input or `/continue` before sampling.
