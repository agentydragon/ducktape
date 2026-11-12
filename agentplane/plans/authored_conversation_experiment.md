# Authored conversation CLI experiment

Specification for a bounded experiment; no implementation or run results yet. The
[task DAG](task_dag.md#agent_authored_conversations_experiment--try-an-authored-conversation-in-a-cli)
owns its scheduling. The experiment informs the broader conversation decision without choosing
Agentplane's eventual history architecture.

## Problem and hypothesis

Current annoyances:

- Native Codex and Claude Sessions cannot currently resume from History alone: their retained
  harness state/PVC is still required. A readable transcript is not a continuation artifact.
- Central sampler already owns durable conversations, model context and execution evidence, then
  exports another representation to History. That duplicates writes, storage and ownership concerns.
- History gives different harness observations a common presentation, although they expose different
  facts: complete tool calls versus executed processes, for example. The common view cannot supply
  details a harness never reported.

Hypothesis: an agent can naturally communicate with a user through a shared thread that it reads
and contributes to using tools. That thread need not mirror model context or execution steps.
Harness transcripts could then be a secondary diagnostic view, with a faithful JSON rendering
sufficient for the experiment. Useful live progress and folding streamed extensions into their
parent item remain desirable regardless of whether this hypothesis succeeds.

Today's Agentplane `Thread` is a projection of Session history
([layering](../docs/thread_layering.md#authorities-and-representations)). This experiment's authored
thread is a separate object; using the word does not change that existing contract.

## Smallest setup

One Python CLI process under `agentplane/x/authored_conversation/`, owning sampling, context and
tool execution. Use Qwen on the operator's Ollama through LiteLLM's OpenAI-compatible
`/v1/chat/completions` endpoint. Configure the base URL, model route and credential source; discover
the available route rather than pinning an assumed model tag. A direct Ollama connection is optional,
not a second required adapter. Reuse existing client/tool helpers where they fit this wire contract.

The CLI needs no Agentplane app, History, Dispatcher, Notifications, Kubernetes resources, browser,
native Codex/Claude harness or MCP server. Direct function dispatch is sufficient. Its model endpoint
is its only service dependency.

Keep three representations separate:

| Representation  | Contents and lifetime                                                                                                   |
| --------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Authored thread | SQLite messages with stable ID, ordered position, author and body; survives context reset and process restart.          |
| Working context | The system instructions, notices, model responses and tool results supplied to the model; initially in memory.          |
| Execution trace | Local JSONL model responses, tool calls/results and input notices, correlated to thread message IDs and sampling steps. |

Record actual model/provider fields needed to understand a run, including streamed tool arguments.
Never execute an incomplete tool call. Keep credentials and authorization headers out of artifacts.
Full repeated request/context snapshots are not required for this experiment.

## Interaction contract

The initial tools are `read_thread(after_message_id?)`, `post_message(body)`, and enough ordinary
work tools to complete a small task, such as file reads and a bounded command runner in an explicit
scratch workspace. There is one thread, one human and one agent. The CLI determines authorship;
the model cannot post as the human. Start with append-only messages.

1. Terminal input commits a human message before scheduling work. Give the model a thread-change
   notice naming the new message, not an automatic copy of the thread. The model reads through
   `read_thread`; its results become part of working context.
2. Sample, collect complete tool calls, execute them, append their results to working context, and
   repeat. Preserve call/result pairing and response fields required by the selected endpoint.
3. `post_message` commits an agent message and prints it in the conversation display. It does not
   wake the agent again. Return the committed message ID as the tool result.
4. Ordinary assistant text and tool output go to the trace. A model response without tool calls
   ends the run and waits for input. If the model omitted the requested published answer, record
   that failure; do not silently promote its ordinary text or force a publishing tool call.
5. Accept human input while work is active. Commit it immediately and announce unread messages at
   the next sampling boundary, including when the current response would otherwise end the run.
   A notice is not proof that the model read or addressed the message. Do not start concurrent
   loops over the same context or automatically repeat a tool effect.

The prompt briefly explains that the thread is shared communication, context is the agent's working
state, and the trace records execution. Do not require a hand-scripted sequence of tool calls.
Set finite model-step and tool-output bounds; report hitting a bound as an incomplete run.

CLI controls:

- `/thread`: display the authored conversation without consulting model context or trace.
- `/trace`: inspect diagnostic events separately; normal display shows authored messages and basic
  busy/error status, without turning raw model output into a second conversation.
- `/reset-context`: while idle, clear working context and reseed only instructions and a notice to
  reread the retained thread. This tests conversational continuity, not restoration of private
  execution state or exactly-once recovery. Reopening a run can use the same fresh-context behavior.

## Runs and win conditions

First establish that the selected route completes an ordinary text exchange and a real
tool-call/result round trip. Record the actual route/model, generation settings and tool schemas.
A failed transport/tool smoke test makes the interaction experiment inconclusive.

Use the same model, generation settings, work tools and equivalent scratch fixtures for two modes:
ordinary chat with direct assistant replies, and the authored-thread interaction above. Start each
mode with fresh context. For the reset scenario, ordinary chat may rebuild context from its visible
conversation as usual; authored mode must retrieve its retained thread through the tool. Run one
paired pass through the scenarios below; repeat a failed or ambiguous case once without changing
its prompt. Record both attempts. These are behavioral examples, not a statistical model benchmark.

| ID  | Scenario                                                                                                                       | Win condition                                                                                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1  | Complete a small file inspection or repair task, then answer a follow-up.                                                      | Work is correct in both modes. In authored mode the requested answer and follow-up appear as agent messages without reminders to use `post_message`.                                            |
| W2  | During a multi-step investigation, ask for a useful progress update, then provide a correction before a later step is sampled. | The agent publishes the requested progress and visibly incorporates the correction at its next opportunity. Every human message remains readable and none is silently stranded when a run ends. |
| W3  | Ask a question requiring clarification before proceeding.                                                                      | The agent asks in the thread, waits for input without spinning, and uses the answer; private trace content is not treated as something the user already saw.                                    |
| W4  | After establishing a decision and an unresolved question in the conversation, reset context and ask what to do next.           | The agent reads the retained thread, recalls the decision and addresses the open question without replaying old commands or reading its old execution trace.                                    |

For a positive result, all four conditions must hold and the operator must be able to conduct the
authored-mode interactions using the thread alone, with useful progress and no repeated coaching
about publishing. The trace may be opened afterward to assess the experiment. A thread dump of
every tool result does not count as useful communication.

Focused deterministic Bazel tests should establish the mechanism: read pagination/order and
authorship, separation of thread/trace/context, delivery of input arriving during a run or at its
end, reset persistence, and reporting a missing published answer. Live Qwen runs establish whether
participation is natural; scripted model responses alone cannot establish that.

## Streaming extension

After the core runs, try one stream into a stable progress item and settle its extensions into the
completed parent body. Check that the final text equals the accumulated extensions, that normal
rendering does not duplicate it, and compare retained bytes before/after settlement. Keep raw
diagnostic evidence for the experiment. Structural folding is distinct from generating a semantic
summary. This extension is reported separately and does not gate the first interaction result.

## Evidence and stopping condition

Keep local run artifacts under the task scratch directory. Add a short findings note under
`agentplane/debug/` with the route/settings and reproduction command, scenario outcomes, relevant
message/trace references, operator observations, model-call/token counts where available, and
retained thread/trace sizes. Report prompting changes and rescue interventions explicitly.

Stop after the bounded paired runs and any single repeats. Classify the result as **promising**,
**unpromising on the tested model**, or **inconclusive**, naming baseline, tooling or provider
limitations. Explain missed publications, execution chatter, unread corrections or dependence on
private context. If
ordinary chat succeeds but authored mode fails, preserve that counterexample rather than changing
the loop until it hides the failure. A success on Qwen is evidence for this interaction only.

The next decision is whether to refine the interaction or try another model/harness. Service
ownership, MCP exposure, participants/editing, History replacement/rename, shared storage,
deduplication/retention and migration remain open. Existing History consumers also use operational
facts such as input confirmation; making transcripts secondary would not remove those obligations.
No Agentplane rollout or deletion of retained data is part of this experiment.
