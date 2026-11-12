# Authored conversation CLI contract

The experiment owns one human/agent conversation per state directory. It samples through a
configured OpenAI-compatible Responses endpoint with streaming and SDK retries disabled. There is
no native harness and no dependency on Agentplane's Session, History or notification services.

## State and presentation

- `thread.sqlite` retains append-only messages with stable UUIDs, ordering and CLI-assigned authors.
  Only explicit `post_message` calls publish assistant messages in authored mode. Model text,
  reasoning and tool results remain diagnostic; they are never promoted into conversation text.
- Working context is in memory. Resetting it preserves the thread and trace. Authored mode starts
  again with instructions and notices, then reads the thread through tools. Chat mode can rebuild
  context from visible conversation messages. Neither mode restores or automatically replays work.
- `trace.jsonl` records complete provider responses, notices, calls/results, publication, reset and
  run outcomes, correlated by session/run IDs and sampling step. Credential values and HTTP headers
  are not recorded. `/trace` is an explicit diagnostic view.

## Input and tools

Human input commits before a run starts. Authored-mode notices identify new human messages without
copying their bodies into context. Input arriving while a sample or tool is active is announced at
the next sampling boundary, including before stopping after a final response. A notice does not
claim that a message was read or addressed. One loop owns context at a time; reset requires idle.
Terminal input setup preserves stdout/stderr flags, including when all three streams share a TTY,
so large diagnostic events remain printable.

`read_thread` returns ordered, bounded pages and an exclusive continuation cursor. `post_message`
commits an agent message before displaying it and returns its ID; publication does not wake the
agent. The model cannot choose a message's author. Chat mode exposes neither conversation tool.

`read_file` reads within the resolved workspace, reports truncation, and refuses symlink escapes.
`run_command` starts a shell in that workspace, bounds time/output, and reaps its process group
on completion, cancellation or a bound. Commands retain ordinary user privileges. Bounds and
errors do not imply that a command had no effects.

## Completion and limits

Only completed provider responses and completed function calls can execute tools. Native reasoning
and call/result items are retained in working context across requests. With default implicit turn
control, a response without calls ends a run only after newly arrived human input has been considered
for announcement. A missing
published reply after the latest announced input is reported as `missing_publication`; the CLI
does not repair it with reminders or publish the model's private text.
`completed` reports loop completion with a publication, not whether that publication answered the
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
