# Authored conversation probes

On November 12, 2026, the initial concise prompt repeatedly left correct answers private on the
tested Qwen route. Both Qwen and Luna published the reset and correction answers with an
explanatory prompt. Each model also revised a prior answer when explicitly instructed to do so.
These bounded probes inform the interaction design. Reliability, live edits during work, and the
broader History architecture remain open.

## Shared method

Use the [CLI instructions](../../x/authored_conversation/README.md) and a fresh SQLite state and workspace
per case. The routes were `ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k` and
`chatgpt-sub/oai-responses/gpt-6-luna` through LiteLLM Responses, with temperature 0, 2,048 output
tokens, 12 model calls, a 600-second request deadline, non-streaming responses, and SDK retries
disabled. Implicit turns use automatic tool choice. For the explicit-yield comparison, set
`turn_control=yield_tool`. The CLI requests `tool_choice=required` even after a publication.
A valid `yield_turn` ends work after its result is recorded, with fresh human input allowing the
run to continue. The CLI rejects tool-free output as incomplete in this mode.

The first paired pass compared authored and ordinary chat with the original concise prompt at
`37c227f26fb799b008cb7f961bf5d2a47ff2d06b`. The fixture used an unchanged `note.txt` containing
`Release codename: Heron.` and a newline. The user decided on short bird names with capitalization
undecided. The reset task asked the model to recover that decision. The correction task asked for
progress before reporting the codename, then requested uppercase while the first sample was in
flight.

For the repeats and later prompt comparison, copy the recorded `position`, `id`, `author` and
`body` into the SQLite `message` table. The fixture contains the conversation through the
unanswered reset question at position 8. Drive reset with `/continue` then `/wait`. For correction,
submit the inspection request and its uppercase correction, then `/wait`. Each case starts with
fresh working context, so the retained messages and input reproduce the test without replaying
the original private context.

The [paired-run record](paired_runs.json.gz) contains run IDs, outcomes,
reported tokens and artifact hashes. The [prompt-comparison record](prompt_comparison.json.gz)
also contains the complete retained-message fixture, exact instructions, tool schemas, settings,
scripts and tool sequences. Read either with `gzip -dc`. The explanatory prompt describes the
thread as an external chat application, makes clear that ordinary assistant text is private,
and explains when to publish progress and final answers. It supplies an unrelated clarification
example. The implicit-turn comparisons changed only `AUTHORED_INSTRUCTIONS`.

## Publication and yield

| Model | Prompt and turn control                | Reset answer                   | Correction answer                                        |
| ----- | -------------------------------------- | ------------------------------ | -------------------------------------------------------- |
| Qwen  | Concise, implicit, failed-case repeats | Private, 3 calls               | Private, 3 calls                                         |
| Qwen  | Explanatory, implicit                  | Published, 4 calls             | Published, 4 calls                                       |
| Luna  | Concise, implicit                      | Private, 2 calls               | Published, 4 calls                                       |
| Luna  | Explanatory, implicit                  | Published, 3 calls             | Published, 5 calls                                       |
| Qwen  | Explanatory, explicit yield            | Published and yielded, 4 calls | Separate progress and final posts, then yielded, 5 calls |
| Luna  | Explanatory, explicit yield            | Published and yielded, 3 calls | Combined progress and final post, then yielded, 4 calls  |

In the original concise Qwen run, the initial answer and the clarification were published.
The reset and correction runs retrieved the relevant facts and composed appropriate answers
without `post_message`. Each omission recurred in one bounded repeat. The ordinary-chat control
showed both answers, combined progress and final output, and sometimes invented details. No
reminders or automatic promotion of private text repaired the misses. Published reset answers
kept the naming decision and uncertainty about capitalization. Published corrections reported
`HERON` and left the file unchanged. Qwen's explicit-yield correction also presented the
unapproved `TERN` suggestion as a decision.

Luna echoed `required` on every explicit-yield request. Qwen echoed `auto` despite recorded
requests for `required`. The record's `required_choice_counterexample` ends with a tool-free
ordinary-text answer after three required requests. Qwen used the explicit protocol in the
final probes. The counterexample leaves enforcement on that route unproved, and these traces
cannot locate the translation boundary that caused it.

The original paired pass used 21 authored versus 11 chat model calls. Reported input/output
tokens were 43,630/5,119 versus 16,620/4,998. Trace sizes were 174,328 versus 97,209 bytes, and
SQLite sizes were 12,288 versus 20,480 bytes. These are fixture measurements subject to SQLite
allocation and missing publications. They do not establish relative storage or latency at scale.

## Editing a prior answer

The editing probe used [PR #15923](https://github.com/agentydragon/ducktape/pull/15923), tested at
`154b6798c4` and rebased to `9d0855b09c` with the same experiment code. Use explicit yield and
a fresh `note.txt` with `Color: blue` and `Count: 3` on separate lines. Ask the model to publish a
two-line summary that can be revised. After it yields, edit the original human message to ask for
double the file count and to update the existing summary without adding a message. Use
`/edit <id> 1 <replacement>`, then inspect `/revisions <id>` and `/thread`. Neither phase changed
the workspace or needed rescue prompting.

| Model | First publication                   | Revision                                                                          | Extra publication                      |
| ----- | ----------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------- |
| Luna  | `Color: blue` / `Count: 3`, 4 calls | Same answer ID and position, revision 2 to `Count: 6`, 5 calls                    | Appended an unnecessary acknowledgment |
| Qwen  | `Color: blue` / `Count: 3`, 4 calls | Same answer ID and position, revision 2 to `Count: 6 (file value 3 × 2)`, 3 calls | None                                   |

Qwen's first request took about 266 seconds. GPU warm-up was not measured separately. Both
models reread the edited instruction and yielded after revising. This tests an explicitly
requested edit after the first run ended. Edits during sampling or following a context reset
remain separate live cases.

## Verification and evidence limits

The [baseline fixture tests](https://app.buildbuddy.io/invocation/59acf179-ddbb-4edb-9e36-f628f507b4d1)
cover authorship, reset, limits, publication and no replay after failed requests. The
[yield tests](https://app.buildbuddy.io/invocation/39f1be3e-e94e-45cc-922a-bf34897e672f)
cover serialized required choice, invalid and premature yield, missing publication, and input
arriving during yield. The editing probe also passed deterministic concurrent/stale edit,
revision restart, active-sample edit, reset/reread, and interactive terminal tests. The bounded
live results establish the observed behavior only for the recorded routes, prompts and cases.

The [original paired report][paired], [prompting report][prompting], and [editing report][editing]
retain the full narrative, including edit message IDs, per-run measurements and exact original
fixture commands.

[paired]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_authored_conversation_cli.md
[prompting]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_authored_conversation_prompting.md
[editing]: https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_authored_conversation_editing.md
