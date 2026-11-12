# Authored conversation editing probe

The CLI now supports human and agent edits with immutable revisions. This probe checks whether
models use that interface after the human changes an instruction they already read. It does not
choose an Agentplane History architecture.

## Setup and scenario

Implementation: PR #15923, initially tested at `154b6798c4`, rebased to `9d0855b09c` without
changing the experiment code. Both routes use `https://litellm.allegedly.works/v1`, authored mode,
`turn_control=yield_tool`, temperature 0, 12 steps, 2048 output tokens and a 600-second request
timeout. SDK retries and streaming are disabled. The existing cheap-experiments credential is
referenced through a private file; no key material is in these artifacts.

Run the [CLI](../x/authored_conversation/README.md) with a fresh workspace and state directory.
Create `note.txt` containing:

```text
Color: blue
Count: 3
```

Send:

> Read note.txt and publish a compact two-line summary with labels Color and Count. Treat this as
> a summary we can revise later.

After completion, edit that human message in place with `/edit <id>` or the piped form
`/edit <id> 1 <replacement>`. The replacement is:

> Read note.txt and publish a compact two-line summary with labels Color and Count. Correction:
> report Count as double the file count. Update your existing summary instead of adding a new
> message.

Check that the model retrieves the changed instruction, uses `edit_message` to revise its original
answer, retains its message ID/position, and yields. Inspect `/revisions <id>` and `/thread` to
compare the original and current bodies. There are no rescue messages or changes to the system
prompt between phases. The task explicitly requests revision; this does not test unsolicited
self-correction. Neither phase executes a command or changes the workspace file.

Local artifacts are under
`/tmp/agents/2026_11_12/authored-conversation-experiment/live-editing/{luna,qwen}/`, including
`config.json`, `stdout.log`, `stderr.log`, `state/thread.sqlite` and `state/trace.jsonl`.
The temporary driver runs the actual Bazel CLI and submits the edit through its command interface;
its SQLite access only reads the human message ID.

## Luna

Route: `chatgpt-sub/oai-responses/gpt-6-luna`.

The initial run read the human message and file, posted `Color: blue` / `Count: 3`, then yielded
in four model calls. After the human edited the original request to revision 2, Luna read that
message again, read the thread, and edited its original answer to `Color: blue` / `Count: 6`.
The answer retained ID `a2720d0d-3a38-4cf0-8b61-6a83a871f006` and became revision 2. The revised
run took five calls and yielded normally.

Luna also appended “Updated the existing summary to reflect the corrected count.” This was
unnecessary and violated the task's request to update the existing summary instead of adding a
message. Revision discovery and publication worked in this case; exact conversation behavior did
not fully meet the request. No corrective prompting was applied.

Across both runs, the provider reported 16,752 input and 255 output tokens, including repeated
working context across requests. The thread contains one human message and two agent messages,
five total revisions. SQLite occupies 24,576 bytes; the JSONL trace occupies 121,051 bytes.
These are one-run measurements, not a performance or reliability estimate.

## Qwen

Route: `ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k`.

The first response arrived about 266 seconds after sampling began, within the configured cold-start
allowance. This records request latency; GPU warm-up was not measured separately.
The initial run read the thread and file, posted `Color: blue` / `Count: 3`, then yielded in four
calls. After the human edited the original request, Qwen called `read_message`, `edit_message`
and `yield_turn` in three calls. The existing answer, ID `30d8351f-d52f-4b96-a1fd-47d73c60dbbb`,
became revision 2 with `Color: blue` / `Count: 6 (file value 3 × 2)`. No additional agent message
was appended. It followed the requested revision behavior in this case, without rescue prompting.

Across both runs the provider reported 11,896 input and 481 output tokens. The thread contains
one human message and one agent message, four total revisions. SQLite occupies 24,576 bytes;
the JSONL trace occupies 76,650 bytes. The route's previously observed uncertainty around API
validation of required tools remains separate from these successful tool calls.

## Scope of evidence

The four focused Bazel targets passed, including a legacy database upgrade retaining exact
bodies/IDs, concurrent stale edits, cross-author refusal, revisions across reopen, an old human
message edited during a final sample or yield, an agent editing an older answer to satisfy new
input, reset/reread retaining the corrected conversation without replay, and Prompt Toolkit
multiline editing/cancellation. The real shared-terminal 1 MiB trace regression passed with the
new interactive path too.

Those deterministic checks establish mechanics. The live runs above establish one instructed
revision case per model, after the first run finished. They do not establish live behavior during
active work or after reset, repeated reliability, natural unsolicited edits, reactions, or whether
broader History changes should follow.
