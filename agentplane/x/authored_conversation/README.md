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

| Command          | Effect                                                                               |
| ---------------- | ------------------------------------------------------------------------------------ |
| `/thread`        | Show all retained human and agent messages.                                          |
| `/models`        | List routes accessible with the configured key, for selecting `model` in the config. |
| `/trace`         | Show raw diagnostic events from `trace.jsonl`.                                       |
| `/reset-context` | While idle, clear working context while retaining the thread and trace.              |
| `/continue`      | Start another bounded run, including after reopening retained state.                 |
| `/wait`          | Finish the active run before reading another input line; useful in piped scenarios.  |
| `/quit`          | Exit and cancel active work. EOF instead drains active work before exiting.          |

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
