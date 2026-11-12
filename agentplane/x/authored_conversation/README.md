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

Type a message and press Enter. You can submit another message while the model works. The next
model request receives a notice naming it. Published agent messages appear in the terminal as
complete messages, along with run status. Ordinary model text is visible through `/trace`.
The interactive prompt supports cursor editing, Up/Down input history and Tab completion for
commands and message IDs. Background output appears above your draft. Ctrl+C cancels the draft;
Ctrl+D on an empty prompt drains active work and exits.

| Command           | Effect                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------- |
| `/thread`         | Show all retained human and agent messages.                                                 |
| `/edit <id>`      | Edit your own message in a prefilled multiline draft; Esc then Enter saves, Ctrl+C cancels. |
| `/revisions <id>` | Show every retained version of a message, including its recording time.                     |
| `/help`           | Show commands and editing keys.                                                             |
| `/models`         | List routes accessible with the configured key, for selecting `model` in the config.        |
| `/trace`          | Show raw diagnostic events from `trace.jsonl`.                                              |
| `/reset-context`  | While idle, clear working context while retaining the thread and trace.                     |
| `/continue`       | Start another bounded run, including after reopening retained state.                        |
| `/wait`           | Finish the active run before reading another input line; useful in piped scenarios.         |
| `/quit`           | Exit and cancel active work. EOF instead drains active work before exiting.                 |

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
