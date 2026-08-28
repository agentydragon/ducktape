# Fixture provenance and sanitization

Every file says what kind of evidence it is in its filename. Never relabel a schema-derived fixture
as a real capture.

- `schema_derived_turn.synthetic.jsonl` is **synthetic**. Its notification and item shapes were
  transcribed from the generated 0.144.1 TypeScript schemas listed in
  `../docs/protocol_evidence.md`. It deliberately covers command and MCP lifecycles even when a
  safely credentialed run does not produce both.
- `real_text_command.sanitized.jsonl` is a **real** `codex app-server` stdio exchange captured on
  2026-08-19 UTC from the pinned agent-workspace image (`codex-cli 0.144.1`). It contains two real
  turns: a text-only `TRACE_TEXT_OK` answer, then a shell `printf TRACE_CMD_OK` command followed by
  `TRACE_COMMAND_DONE`. It was staged at `.openclaw/codex-trace-4431/`; before commit, fixed
  prompts and remaining absolute paths were replaced with explicit placeholders. The raw trace
  remains in the ephemeral sandbox and is not part of this PR.

  The capture used the existing in-cluster LiteLLM Responses provider and an injected credential,
  but no credential was read, copied, printed, or serialized. Prompts forbade file, environment,
  credential, and network access. Paths, timestamps, process IDs, and native IDs were sanitized by
  the capture workflow before staging. The provenance notes in `.openclaw/codex-trace-4431/README.md`
  are the source record for this fixture.

- `real_provider_failure.sanitized.jsonl` is a **real** production session, excerpted from the
  haku-console frame log on 2026-08-26 UTC: a Web-launched `public-coder-agent` Codex conversation
  whose single turn failed after five provider retries (issue #4752). The capture program cannot
  produce it — it cannot induce a provider outage — so the source is the durable frame log rather
  than a staged run, and every frame of that session is present, in order.

  Sanitization ran `capture.py`'s own `Sanitizer` over each frame, then replaced the two identities
  it has no rule for: the console session ID inside `remoteControl/status/changed`'s `serverName`,
  and that notification's `installationId`. Wall-clock epochs were normalized to the same
  `1700000001` base as the fixture above, preserving the turn's real 189-second span. Two artifacts
  of the reviewed sanitizer are visible and deliberate: the `configWarning` summary's
  `https://developers.openai.com/...` URL is rewritten to `<ABSOLUTE_PATH>`, because the path regex
  cannot tell a URL from a filesystem path; and the operator's prompt, the two-letter `hi`, was
  lifted to a sentinel before sanitizing, since substring replacement of `hi` would otherwise have
  eaten the `hi` inside `high` and `which` in the provider's own error text.

  Two further sessions that day failed identically, frame for frame apart from IDs and timings, so
  this shape is the repeatable one rather than a single anomaly.

The capture program writes sanitized records only. Before committing a real capture, review every
line for all of the following:

1. no authorization headers, API keys, tokens, cookies, credentials, or environment values;
2. no real user text, repository contents, usernames, hostnames, absolute paths, or tool output;
3. thread/turn/item/process/client IDs replaced by stable placeholders;
4. only the intended bounded initialize → thread/start → turn/start → turn/completed exchange;
5. no stderr (the utility drains and discards it).

A real fixture should use a disposable directory and a fixed prompt whose expected output is safe
to publish. If any line is uncertain, omit the fixture and document the case as synthetic instead.
