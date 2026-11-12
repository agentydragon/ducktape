# Authored conversation CLI: bounded Qwen run

## Result

**Unpromising on the tested Qwen/LiteLLM configuration.** The CLI mechanism worked, and Qwen
initially published answers and a clarification through `post_message`. After context reset it
retrieved the conversation and composed an appropriate answer, but left that answer in ordinary
model output. It also omitted publication in the concurrent-correction scenario. Both omissions
occurred again in one bounded repeat. The user would need `/trace` to see those answers.

No reminders, forced tool choice, automatic promotion of private text, or prompt changes were used
to repair the omissions. This is a negative interaction result, not a failed implementation test.
It does not establish whether the cause is model behavior or provider translation, nor whether
another model would behave similarly.

Implementation: [PR #15896](https://github.com/agentydragon/ducktape/pull/15896),
[CLI contract](../x/authored_conversation/SPEC.md), [run instructions](../x/authored_conversation/README.md).

## Setup

- Endpoint: `https://litellm.allegedly.works/v1/responses`.
- Route discovered through the existing cheap-experiments key's model list:
  `ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k`.
- Both modes: temperature `0`, `max_output_tokens=2048`, `max_steps=12`, request timeout `600s`,
  default command/output bounds, non-streaming Responses, SDK retries disabled, `tool_choice=auto`.
- Independent workspaces each contained only `note.txt`, with `Release codename: Heron.` and a
  newline. The note's SHA-256 remained
  `6a382c0ef7a2e8feaa56a8834da03da265e8891dd1155ed1b521326e94bc5174` in every run.
- A successful four-call authored smoke run preceded the paired pass, allowing the cold model
  additional warm-up time. No direct Ollama or second-model comparison was run.

Use the component README's configuration with these overrides and separate state/workspace
directories for `authored` and `chat`. Save this input as `paired-input.txt`:

```text
Read note.txt and tell me the release codename.
/wait
Before choosing our next release codename, find out whether I want bird names or plant names.
/wait
Use short bird names. Leave capitalization undecided; we will decide it later.
/wait
/reset-context
What naming decision did we make, and what is still undecided?
/wait
Inspect note.txt again and give me a short progress update before the final result. Tell me the release codename.
Correction for that investigation: the final codename must be in ALL CAPS. Do not change any files.
/wait
```

```bash
cat /path/to/paired-input.txt | bb run //agentplane/x/authored_conversation:main_bin -- --config /path/to/config.json
```

## Observations

| Case                                    | Authored thread                                                                                                                                   | Ordinary chat control                                                                                                                                                                         |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1: file task and follow-up             | Correct `Heron` answer was published. It then published a redundant wrap-up about nonexistent TODO/spec files.                                    | Correct visible `Heron` answer in two calls; it added an unsupported claim about an embedded instruction.                                                                                     |
| W3: clarification and preference        | Asked birds versus plants, then published an acknowledgement of short bird names and deferred capitalization.                                     | Asked the same preference. The subsequent preference input produced no visible acknowledgement and was reported as `missing_publication`; the later answer nevertheless used that preference. |
| W4: reset and reread                    | Read the thread and inspected the note, then answered correctly only in private output. `missing_publication`, three calls.                       | Visible answer retained short bird names and deferred capitalization. One call.                                                                                                               |
| W2: progress and correction during work | Read the new messages and produced an appropriate `HERON` answer/progress description only in private output. `missing_publication`, three calls. | Visible `HERON` answer honored the correction and left the file unchanged. Progress and final result were combined in one reply, rather than separate preliminary progress.                   |

The authored reset failure was run `557b1d03-e1ff-47e7-a572-d20136466147`; the correction failure was
`852534e7-435b-47d8-b0c8-0506c0bd7082`. In both, the trace contains ordinary assistant messages but
no `post_message` call. For example, the reset answer privately identified bird names as decided and
capitalization as undecided. The problem was publication, not absence of the conversation facts.

The correction arrived during a model request in both modes:

| Mode     | Sample started, UTC | Correction committed, UTC | Response recorded, UTC |
| -------- | ------------------- | ------------------------- | ---------------------- |
| Authored | `22:14:31.492634`   | `22:14:31.600953`         | `22:14:33.906392`      |
| Chat     | `22:18:16.381498`   | `22:18:16.470594`         | `22:18:20.548909`      |

The authored repeat fixtures preserved the original retained messages/IDs through the reset
question in independent state directories. The reset repeat used `/continue` with that retained
question; the correction repeat submitted the same last two human messages from a fresh context.
The latter therefore repeated the user wording and retained conversation, not the exact original
private working context. Both ended `missing_publication` after three calls, with no prompt changes.
Original state was retained. This was one paired pass and one repeat of each authored failure, not
a statistical reliability estimate.

The model also made unnecessary workspace probes, once supplied a numeric rather than UUID thread
cursor and corrected it after the tool error, and sometimes invented or overgeneralized details.
The chat control inspected its neighboring diagnostic state during clarification; the authored
reset case did not read the old trace. These are additional limits on treating this small task as
a clean measure of conversation quality. No known API credential values appeared in the traces.

## Evidence and checks

| Paired pass | Model calls | Reported input tokens | Reported output tokens | Trace bytes | SQLite bytes |
| ----------- | ----------- | --------------------- | ---------------------- | ----------- | ------------ |
| Authored    | 21          | 43,630                | 5,119                  | 174,328     | 12,288       |
| Chat        | 11          | 16,620                | 4,998                  | 97,209      | 20,480       |

These are provider-reported token counts and physical artifact sizes for this fixture. SQLite
allocation and missing publications affect the sizes; they do not establish a storage or latency
advantage. No retention/architecture decision follows from them.

[Run summaries](2026_11_12_authored_conversation_cli.json) retain run IDs, outcomes, token counts,
file sizes and artifact hashes. Raw artifacts were kept under
`/tmp/agents/2026_11_12/authored-conversation-experiment/` in `live-authored`, `paired-authored`,
`paired-chat`, `repeat-reset` and `repeat-correction`; each has `state/{thread.sqlite,trace.jsonl}`
and a workspace. Scratch files are temporary; the summaries and observations above are the durable
record. Credential contents and raw reasoning are not committed.

Focused Bazel validation, including library lint/type aspects, passed:
[invocation 59acf179](https://app.buildbuddy.io/invocation/59acf179-ddbb-4edb-9e36-f628f507b4d1).
All four test targets passed, covering SQLite persistence/paging/authorship, private versus published
output, late input, reset, step/output/time limits, incomplete responses, duplicate effects, failed
requests without replay, and the piped CLI. These checks establish the mechanism; the live
publication failures remain visible evidence for the next design decision.
