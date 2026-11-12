# Authored conversation: explanatory prompting and explicit yield

## Result

**Clearer prompting fixed the observed missing publications in these probes.** Both Qwen and Luna
published the reset and correction answers with the explanatory prompt. Both also published and
called `yield_turn` in the always-required-tool variant. The first negative result was specific to
the concise prompt; these small probes do not establish publication reliability or task quality.

Luna uses `chatgpt-sub/oai-responses/gpt-6-luna` on the same LiteLLM endpoint/key. Qwen remains
`ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k`.

| Model | Prompt and turn control                        | Reset answer                     | Correction answer                                   |
| ----- | ---------------------------------------------- | -------------------------------- | --------------------------------------------------- |
| Qwen  | Concise, implicit; earlier failed-case repeats | Private; 3 calls                 | Private; 3 calls                                    |
| Qwen  | Explanatory, implicit                          | Published; 4 calls               | Published; 4 calls                                  |
| Luna  | Concise, implicit                              | Private; 2 calls                 | Published; 4 calls                                  |
| Luna  | Explanatory, implicit                          | Published; 3 calls               | Published; 5 calls                                  |
| Qwen  | Explanatory, explicit yield                    | Published, then yielded; 4 calls | Progress and final posts, then yielded; 5 calls     |
| Luna  | Explanatory, explicit yield                    | Published, then yielded; 3 calls | Combined progress/final post, then yielded; 4 calls |

Published reset answers retained short bird names and explicitly undecided capitalization.
Correction answers reported `HERON` and left the file unchanged. All three implicit-turn correction
cases that published combined progress and the final result in one post. Luna's concise run also
produced an invisible ordinary-text progress announcement before work. Qwen's explicit-yield run
used separate progress and final posts; Luna still combined them. Qwen's final post also presented
its unapproved `TERN` suggestion as the next release codename. Publication and turn completion do
not establish task correctness.

## Explanatory prompt

The explanation describes the thread as a chat application outside model context, spells out that
even a final assistant response is invisible to the human, and explains that ending a turn sends
nothing. It gives one unrelated example of publishing a clarification question and explains how to
post progress, keep working, publish an answer and stop without duplicating it. It supplies no
answer to the evaluation tasks and prescribes no fixed tool sequence.

The explanatory-versus-concise comparison uses no automatic reminders, forced tool calls or
promotion of ordinary text. Its only behavior change is `AUTHORED_INSTRUCTIONS` in
[agent.py](../x/authored_conversation/agent.py). The explanatory prompt is now the default.

## Always-required tools and explicit yield

The operator also requested a protocol in which every response calls a tool, including yielding.
Set `turn_control=yield_tool`: every request sends `tool_choice=required`, including after posting.
The tool set adds `yield_turn({})`, and instructions explain publishing versus yielding. Ordinary
text cannot end a run. A valid yield ends work after recording its result, or continues if fresh
human input arrived. The implicit-turn comparison remains available as the default.

Luna echoed `required` on every response. Qwen echoed `auto` despite every recorded request
selecting `required`. An intermediate concise-prompt probe, retained as
`required_choice_counterexample` in the comparison record, requested `required` on all three
samples but received a final ordinary-text response with no tool call. Thus API enforcement is
not established on this Qwen route. These traces do not isolate whether LiteLLM, Ollama or their
translation drops or fails to enforce the choice. Qwen did follow the explicit-yield protocol in
the final probes; the loop would reject a tool-free response as incomplete rather than treating
it as a yield. Deterministic tests check the serialized SDK requests and that rejection.

## Method and reproduction

Each case starts with fresh working context and an independent copy of the original retained
thread through the unanswered reset question (position 8). The shared fixture includes the earlier
human decisions and agent messages, with their original IDs. Each workspace has the same unchanged
`note.txt`. This is the same fixture used for the original failed-case repeats, not a replay of
their private execution context.

- Reset: submit `/continue`, then `/wait`.
- Correction: submit the same inspection/progress request and ALL CAPS correction from the
  [first run](2026_11_12_authored_conversation_cli.md), then `/wait`. The correction is committed
  while the first model request is in flight in each case.

The [comparison record](2026_11_12_authored_conversation_prompting.json) contains the complete
retained-message fixture, input scripts, exact instructions, tool schemas, generation settings,
routes, run IDs, tool sequences, publications, token counts and artifact hashes. Use the
[CLI instructions](../x/authored_conversation/README.md) with each recorded route, an existing
credential, a fresh state/workspace pair, and the relevant input script. Copy the fixture's
`position`, `id`, `author` and `body` rows into the SQLite `message` table before starting. The
concise version is in commit `37c227f26fb799b008cb7f961bf5d2a47ff2d06b`; the explanatory and explicit-yield
versions are the current implementation. Select `turn_control=yield_tool` only for the yield variant.

Earlier implicit-turn traces predate the per-sample requested-choice field; their null entries
mean it was not recorded. The explicit-yield traces record `required` for every sample.

All runs use temperature 0, 2048 output tokens, at most 12 model calls, non-streaming Responses,
no SDK retries and a 600-second request deadline for GPU warm-up. Implicit-turn runs use automatic
tool choice and unchanged tools/delivery/termination behavior. Workspace contents are unchanged.
The follow-up makes one attempt per listed prompt/model/protocol/case combination; the original
Qwen concise cases had already failed in both the paired pass and their single repeats.

## Scope and checks

These are targeted probes, not a fresh complete W1-W4 comparison or a reliability benchmark.
Preliminary progress remains inconsistent, and the ordinary-chat control also had model-quality
limitations. The earlier “unpromising” label applies to the original prompt rather than the
whole authored-thread interaction. The broader History and optional-thread architecture remains
undecided.

Focused tests and library lint/type checks passed:
[invocation 39f1be3e](https://app.buildbuddy.io/invocation/39f1be3e-e94e-45cc-922a-bf34897e672f).
Coverage includes tools remaining required after publication, explicit yield, ordered
publication/yield batches, rejection of work after yield, invalid yield arguments, missing
publication, step bounds and input arriving during yield.

Raw artifacts remain under `/tmp/agents/2026_11_12/authored-conversation-experiment/prompt-comparison/`,
in a state/workspace pair for each model/prompt/case. They are temporary; the comparison record is
the durable evidence. No credential contents or raw reasoning are committed.
