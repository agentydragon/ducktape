# Codex app-server runtime adapter

This isolated package parses and projects the app-server protocol shipped by
`@openai/codex@0.144.1`. It implements the same Console runtime and shared-runner seams as Claude,
but remains unconfigured for production execution: it adds no deploy runtime selection, sandbox
namespace, credentials, or conversation writer.

The committed `testdata/real_text_command.sanitized.jsonl` is a real, reviewed capture from
`codex-cli 0.144.1` (two bounded turns: text-only and command execution). Its capture and
sanitization provenance is recorded in `testdata/README.md`; `testdata/schema_derived_turn.synthetic.jsonl`
is synthetic schema coverage and must not be described as observed wire evidence.

## Capture a real sanitized trace

Run inside a disposable credentialed Codex workspace with a fixed, reviewable prompt:

```sh
bbr run //haku/console/x/codex_app_server:capture_bin -- \
  --codex /path/to/pinned/codex \
  --cwd /disposable/workspace \
  --output /tmp/codex-app-server.sanitized.jsonl \
  --prompt 'Reply with exactly TRACE_OK. Do not inspect files or run commands.'
```

For a command lifecycle, use an equally bounded prompt that names a harmless command and expected
literal output. MCP lifecycle capture additionally requires a deliberately configured safe MCP
server; do not add credentials or MCP configuration to this package.

The utility:

- launches `codex app-server --listen stdio://`;
- performs `initialize`/`initialized`, `thread/start`, and `turn/start`;
- records direction-labelled JSONL through the matching `turn/completed`;
- drains but never records stderr;
- records no environment block and substring-replaces inherited environment values, skipping values
  shorter than 12 characters (not credential material, and replacing them corrupts unrelated text);
- replaces the prompt whole at the prompt-bearing protocol paths (`turn/start` input, `userMessage`
  content), so a short prompt like `hi` cannot mangle other frame text;
- replaces workspace paths — refusing a workspace shorter than 12 characters — native IDs,
  credential-shaped keys, bearer values, and OpenAI-key-shaped strings before writing.

Sanitization is not a substitute for review. Follow `testdata/README.md` before committing output.
