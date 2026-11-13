# Authored conversation CLI

Local experiment with our own non-streaming Responses API loop, Qwen or Luna through LiteLLM, a SQLite
conversation, and separate JSONL diagnostics. No deployed Agentplane services are required beyond
the model endpoint. [SPEC.md](SPEC.md) defines interaction and failure behavior.

## Run

Create a JSON configuration file, using an existing key with access to the chosen LiteLLM route:

```json
{
  "base_url": "https://litellm.allegedly.works/v1",
  "model": "replace-with-an-accessible-qwen-route",
  "credential": { "kind": "environment", "name": "EXPERIMENT_API_KEY" },
  "workspace": "/absolute/path/to/scratch-workspace",
  "state_directory": "/absolute/path/to/run-state",
  "mode": "authored",
  "request_timeout_seconds": 600
}
```

`workspace` must already exist. Commands execute with your user privileges; it sets their working
directory, not an isolation boundary. Use a scratch copy for work that changes files. Credentials
can instead use `{"kind": "file", "path": "/path/to/existing-key"}`. The config file is the only
settings source; it rejects unknown keys. Credential contents are never added to the trace.
The example allows ten minutes for a cold Ollama model to warm up; command deadlines are separate.

```bash
bb run //agentplane/x/authored_conversation:main_bin -- --config /absolute/path/to/config.json
```

With terminal input and output, this opens a full-screen Textual app. **Thread** shows the retained
conversation immediately, with Markdown bodies and author/revision headers. New messages append;
edits update the existing message in place. Type in the composer: Enter adds a line and **Ctrl+S**
sends. You can send another message while the agent works; the next model request receives its
notice. A spinner, elapsed time and model-call number distinguish active work from idle.

**Trace** shows retained diagnostic events in a table and the selected event's full JSON. It follows
new events while open, preserving a selected older event. Ordinary model text and tool activity
appear here. The thread contains only authored messages.

| Control       | Effect                                                                                  |
| ------------- | --------------------------------------------------------------------------------------- |
| F2 / F3       | Switch to Thread / Trace.                                                               |
| Ctrl+S / Send | Send the composer; Enter inserts a newline.                                             |
| Edit          | Open a human message's Markdown source in a separate editor. Ctrl+S saves; Esc cancels. |
| Revisions     | Inspect all versions of a message; Esc closes.                                          |
| F5            | Continue retained work or start another bounded run.                                    |
| Ctrl+R        | Reset model context while idle, preserving the thread and trace.                        |
| Ctrl+Q        | Quit and cancel active work.                                                            |

Editing preserves the new-message draft. Stale edits stay open with their source intact, so they can
be cancelled and reconsidered against the current revision. The stored body remains Markdown source.

If either input or output is redirected, the CLI accepts one message or command per line. Message
display uses Rich Markdown; diagnostics remain raw JSONL. The following commands are for this
line-oriented mode:

| Command                        | Effect                                                                               |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| `/thread`                      | Show all retained human and agent messages.                                          |
| `/edit <id> <revision> <text>` | Revise your own message, checking its expected revision.                             |
| `/revisions <id>`              | Show every retained version of a message, including its recording time.              |
| `/help`                        | Show commands.                                                                       |
| `/models`                      | List routes accessible with the configured key, for selecting `model` in the config. |
| `/trace`                       | Show raw diagnostic events from `trace.jsonl`.                                       |
| `/reset-context`               | While idle, clear working context while retaining the thread and trace.              |
| `/continue`                    | Start another bounded run, including after reopening retained state.                 |
| `/wait`                        | Finish the active run before reading another input line; useful in piped scenarios.  |
| `/quit`                        | Exit and cancel active work. EOF instead drains active work before exiting.          |

Edits keep message IDs and original positions. The thread shows the latest body with its revision
number and an edited marker. The agent can read particular revisions through `read_message` and
revise its own messages through `edit_message`. Each edit checks the expected revision and refuses
a stale overwrite. Human edits notify the model at its next sampling boundary. Its previous context
and executed commands remain intact; the next read reveals the current text.

For piped input, use `/edit <id> <expected-revision> <replacement text>` on one line. `/wait` remains
a barrier before reading the next input line. Existing databases migrate automatically without
discarding messages; stop the old CLI first. An older binary cannot read the upgraded database.

For the ordinary-chat comparison, use `"mode": "chat"` and separate state/workspace directories.
It exposes the same work tools and renders ordinary assistant replies directly. Keep model and
generation settings equal between paired runs. Reopening state does not automatically run anything.

To try explicit turn control, set `"turn_control": "yield_tool"` in authored mode. Every request
uses `tool_choice=required`, including after publication. The model calls `yield_turn` with `{}`
to end work and wait for input; ordinary text cannot end the run. Publishing and yielding are
separate actions. The default `"turn_control": "implicit"` uses automatic tool choice and ends on
a response without calls. Both variants retain finite step limits and report missing publication.

Defaults: 24 model calls per run, 4096 output tokens per call, temperature 0, 120-second request
timeout, 15-second command timeout, 16 KiB per command/file output, 8000 characters per authored
message, and 20 messages per thread page. These bounds are configurable in [Settings](settings.py).
Model responses and usage are recorded once per call, without repeated full-context snapshots.

## Check

```bash
bbr test //agentplane/x/authored_conversation:all
```

Tests exercise the real SDK against a scripted HTTP boundary and local SQLite/files/processes.
They establish delivery, persistence and separation of representations; they do not establish
natural participation by a live model.

[Live prompting comparison](../../debug/2026_11_12_authored_conversation_prompting.md) records the
original publication failures and the clearer interface explanation tested on Qwen and Luna.
[Live editing probe](../../debug/2026_11_12_authored_conversation_editing.md) records both models
revising an existing answer after a human edit, including Luna's unnecessary extra acknowledgement.
