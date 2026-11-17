# Thread prior art

How other agent systems let an agent talk to people through explicit messages, gathered for the
[Thread spike](thread_spike.md). Two sources, both from 2026-11-17: a research lane's survey, and this
project's own coordinator agent describing the chat it works in. Each claim keeps its source's
label for how it was checked.

## Agent messaging elsewhere

Surveyed by a research lane, 2026-11-17. Labels: **read** means it read the page itself; **excerpt**
means the claim comes from a search excerpt of the docs; **leaked prompt** means an unofficial
prompt copy; **recalled** means its own knowledge of the spec, not a reading.

The pattern has no settled name: "send_message tool" or "message tools" (MemGPT, Manus, Devin), a
"message_tool" reply mode (OpenClaw), "channels" (Claude Code). Research files it under multi-party
conversation, or "when to speak".

### Who uses it

- **MemGPT/Letta legacy agents** (read): the model's text was private inner monologue, and
  `send_message` was "the ONLY action that sends a notification to the user"
  ([prompt copy](https://huggingface.co/datasets/minchyeom/MemGPT-Functions-ChatML/raw/main/train.csv),
  [legacy docs](https://docs.letta.com/guides/legacy/memgpt_agents_legacy)).
- **Letta reversed it** (read): [`letta_v1_agent`](https://www.letta.com/blog/letta-v1-agent/) drops
  `send_message` and heartbeats so the model stays "in-distribution" with its training, and Letta
  reports it works better on GPT-5 and Sonnet 4.5. It gave up tool rules on replies and heartbeats.
  The lane's untested guess: the regression came mostly from pushing reasoning into message text, so
  keeping native reasoning and routing only user-visible output through a tool may do better.
- **OpenClaw** (read), agents in WhatsApp, Telegram, Discord and Slack:
  [`visibleReplies: "message_tool"`](https://docs.openclaw.ai/channels/groups) sends visible replies
  only through `message(action=send)`. Stray final text stays private, with a bounded attempt to
  recover it. A reply of only `NO_REPLY` ends the turn silently.
- **Claude Code [channels](https://code.claude.com/docs/en/channels)** (read; research preview): chat
  messages arrive as `<channel source=… attr=…>` events and Claude answers through a `reply` tool.
  Events arriving while Claude is busy are "delivered together on the next turn"
  ([reference](https://code.claude.com/docs/en/channels-reference)).
- **Manus and Devin** (leaked prompt): Manus has `message_notify_user` (no reply needed),
  `message_ask_user` (waits for the user) and `idle`; Devin has `message_user` with
  `block_on_user_response`
  ([Manus](https://glama.ai/mcp/servers/@JamesANZ/system-prompts-mcp-server/blob/404ce4c27a251680432b073a7eb9ac660a206329/prompts/Manus%20Agent%20Tools%20&%20Prompt/tools.json),
  [Devin](https://elifuzz.github.io/awesome-system-prompts/devin)).
- **This project chat:** see [below](#this-projects-coordinator-chat). The public
  [Projects docs](https://code.claude.com/docs/en/claude-projects) do not describe this part.
- **Not the same pattern:** LangChain's
  [ambient agents and Agent Inbox](https://blog.langchain.com/introducing-ambient-agents) are
  event-triggered agents plus a queue where a person reviews their work; there is no shared room.

### Failure modes

- **Forgetting to send.** The reason OpenClaw recovers private text; the CLI experiment reports it as
  `missing_publication`.
- **Knowing when to speak** (read, abstracts). With several participants it does not come free: eight
  LLMs "consistently fail" zero-shot at deciding whether to speak or stay silent, and fine-tuning
  gained up to 23 points ([Speak or Stay Silent](https://arxiv.org/abs/2603.11409)). Replying on
  every turn causes "excessive interruptions" ([When2Speak](https://arxiv.org/abs/2605.05626)).
- **Forcing a tool call** (excerpt): Claude rejects `tool_choice` `any` or `tool` while extended
  thinking is on
  ([Anthropic docs](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use)).
  Manus forces a mode by prefilling the response instead
  ([post](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus), read),
  which needs control of the raw prompt.

### Patterns

1. **Explicit silence:** a no-reply tool or token (OpenClaw's `NO_REPLY`, Manus's `idle`, the CLI
   experiment's `yield_turn`), so the harness can tell "chose silence" from "forgot to send".
2. **Notify versus ask** (Manus, Devin): each send says whether the agent now waits on the person,
   which yields a "waiting on you" state.
3. **Inbound queue policy:** OpenClaw's
   [`steer`, `followup`, `collect` and `interrupt` modes](https://docs.openclaw.ai/concepts/queue)
   (read) with a 500 ms debounce, a cap of 20 and a `summarize` drop; LangGraph's
   [double texting](https://docs.langchain.com/langsmith/double-texting) (excerpt): reject, enqueue,
   interrupt or rollback. A cap of one notice per 10 minutes is `collect` with a 10-minute window
   plus a summarize drop.
4. **Matrix** (recalled): stable event IDs, edits as new events with an
   [`m.replace`](https://spec.matrix.org/latest/client-server-api/#event-replacements) relation, and
   per-participant [read receipts](https://spec.matrix.org/latest/client-server-api/#receipts), which
   answer "what has the other side seen" in both directions.
5. **Slack** (excerpt): an agent's [status](https://docs.slack.dev/ai/agent-sessions) is set apart
   from its messages, so "working on it" is never a message.
6. **Later, with more participants** (excerpt): mention gating, with unaddressed messages kept as
   context (OpenClaw groups, [Claude Tag](https://claude.com/docs/claude-tag/users/when-claude-responds)).

The lane's three suggestions for the spike are in
[its proposals](thread_spike.md#proposals-from-prior-art).

## This project's coordinator chat

Described by the coordinator agent, 2026-11-17, at the functional level from what it can do and
observe; not a copy of its instructions or exact schemas.

### Shape

- One project chat, the person plus one coordinator agent, and many work threads, each with its own
  agent that has the repository and full tools. The coordinator has no repository; it routes, relays
  and summarizes. To the person it reads as one assistant.
- The coordinator's plain text reaches nobody. It speaks only through explicit actions, and every
  turn ends with one.

### Events in, one per turn

- A person's message in the project chat; any message in any thread (agent replies and the person's
  messages there); a button press on a card, arriving as a message naming the button's action and
  the card; a reaction or edit; a notice that a thread posted a decision card; a subagent finishing.
- Timer events, only after the coordinator ends a turn holding updates: a time limit (about 10
  minutes), quiet (the person stopped writing for a minute), and catch-up (the person has been away
  over 30 minutes).
- Each event carries the message ID, author, time, a trust label (the person speaking to it, the
  person's message shown for context, or another agent or bot), and how long ago the person last
  wrote anywhere. Long bodies are truncated, fetched on demand. After a restart it gets a backfill of
  recent chat, the thread list and project memory.

### Actions out

- **Post** a Markdown message: optional reply-to, an opt-in flag that alerts the person's phone, the
  threads it is about, attached files, links or artifacts, and thread decision cards embedded under
  the text.
- **React and unreact**; silently **edit** its own message, for corrections only.
- **End the turn without replying**, with a reason: nothing to add, not for me, asked to be quiet, or
  holding updates, which schedules the timer events.
- **Decision card:** one question, two to four options each with a one-line consequence, one
  recommended with a reason. A press returns as an event and counts as the person's own answer. A
  card can also be closed with the option the person named in words, or withdrawn. A card posted in
  a thread can be shown again in the project chat, and a press in either place answers it.
- **Richer widgets:** cards with buttons, charts, comparison tables, editable drafts.
- **Threads:** start one (title, instructions, the person's messages attached by ID, an
  acknowledgement line in the chat and a first line in the thread); message one (its own words, with
  the person's messages attachable by ID so they arrive as the person's own, with author and time);
  read a thread, the chat or messages by ID; list threads with state (working, blocked, review ready,
  done, failed, stopped) and their pull requests' states; rename, resolve or reopen, set a reminder.
- **Project memory:** one shared Markdown page, read then replaced whole under a revision check,
  size-capped, also written by threads.
- A scheduled message to itself at a time or after a delay; read-only subagents for lookups; reading
  project files.

### Properties worth borrowing

- Speech is a tool call, and silence is also an explicit, reasoned action. There is no implicit
  reply.
- Authority travels by message ID: an approval reaches a worker only as the person's attached
  message, never as the coordinator's paraphrase. Agent-to-agent messages are requests, not
  approvals.
- Trust labels on every inbound message separate the person from other agents; fetched content is
  data, not instruction.
- Presence-aware delivery: results are held and batched while several threads run or the person is
  away; things that block work go out at once. Interrupting the phone is opt-in per post.
- Decisions are structured objects with a default, answerable wherever the person is.
- The coordinator sees only messages, not workers' contexts, so it stays cheap while tracking many
  threads ([observation](thread_spike.md#observation-coordinating-across-threads)).
- Threads have titles and states the person can scan; posts link a thread rather than replaying it.

### Failure modes it hit on 2026-11-17

- Answering "what next" from stale memory, then correcting it a minute later; the fix is re-reading
  thread state before a status answer.
- Relaying a worker's claim ("ready to merge, known flake") that the worker later revised.
- Too many small posts when events arrive seconds apart; batching rules help, but direct answers
  bypass them.
- A whole-page memory with a size cap makes each small update expensive, so updates get deferred.
- A long lookup keeps the coordinator from seeing the person's next message, so anything long belongs
  in a thread.
- A card answered in words needs explicit reconciliation, or it stays open.
