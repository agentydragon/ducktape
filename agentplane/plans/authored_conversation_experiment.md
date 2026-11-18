# Authored conversation CLI experiment

Specification for a bounded experiment. Prototype source is under
[`agentplane/x/authored_conversation/`](../x/authored_conversation/README.md). The
[publication and yield probes](../debug/authored_conversation/README.md#publication-and-yield) record Qwen's repeated missing
publications under the concise prompt and the later explanatory prompt and explicit-yield
results for Qwen and Luna. Preliminary progress remains inconsistent. The
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
sufficient for the experiment. Progress updates are complete authored messages. Streaming input
display, incremental rendering and delta settlement are outside this experiment.

Today's Agentplane `Thread` is a projection of Session history
([layering](../docs/thread_layering.md#authorities-and-representations)). This experiment's authored
thread is a separate object; using the word does not change that existing contract.

Authored threads are optional in eventual Agentplane designs. An agent may instead work in the
background with other tools, inputs and outputs, while the app presents its relatively raw
transcript/logs. This experiment does not make a conversation view a Session prerequisite.

## Runs and win conditions

First establish that the selected route completes an ordinary text exchange and a real
tool-call/result round trip. Record the actual route/model, generation settings and tool schemas.
A failed transport/tool smoke test makes the interaction experiment inconclusive.

Use the same model, generation settings, work tools and equivalent scratch fixtures for two modes:
ordinary chat with direct assistant replies, and the authored-thread interaction in
[`SPEC.md`](../x/authored_conversation/SPEC.md). Start each mode with fresh context. For the reset
scenario, ordinary chat may rebuild context from its visible conversation as usual; authored mode
must retrieve its retained thread through `read_thread`. Run one paired pass through the scenarios
below; repeat a failed or ambiguous case once without changing its prompt. Record both attempts.
These are behavioral examples, not a statistical model benchmark. Do not require a hand-scripted
sequence of tool calls in the prompt.

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

An optional explicit-turn comparison, requested after the first runs, sets
`turn_control=yield_tool` in authored mode. Every request requires a tool, including after publication;
the agent ends work and waits for human input by calling `yield_turn({})`. Publishing and yielding
are separate actions. Keep finite step limits, handle input arriving during a yield, and reject
ordinary text as a turn-ending response in this variant. Record these results separately from the
implicit-turn protocol; enforcement does not establish the original natural-publication gate.

## Follow-on interaction experiments

After the core runs, try richer ways for participants to act on the thread. These are candidates,
not additional gates for W1-W4:

- **Emoji reactions:** let either participant react to a message. Try an acknowledgement without
  another text message, and user feedback on an agent answer. Observe whether the model naturally
  uses and understands reactions. Compare notifying it of a reaction with discovering one on a
  later read before choosing wakeup behavior or fixed emoji meanings.
- **Editing past messages:** the CLI now supports revisioned edits by both participants. The first
  [live revision probe](../debug/authored_conversation/README.md#editing-a-prior-answer) showed Qwen and Luna
  revising an existing answer after the human edited an earlier instruction; Luna also appended
  an unnecessary acknowledgement. Next try edits during live work and rereading after a context
  reset. Check whether the agent proceeds from the corrected conversation without treating stale
  context as current, undoing effects, or replaying completed work.

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
