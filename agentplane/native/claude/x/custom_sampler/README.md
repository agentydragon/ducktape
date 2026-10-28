# Custom Claude sampler experiment

A small Anthropic Messages sampler drives an `append_file` tool in a separate executor process.
The sampler assembles SSE responses and sends assistant content plus `tool_result` continuation.
Complete provider blocks retain their fields, including thinking signatures; unknown incremental
delta formats fail explicitly. Tools run only after `message_stop`. This does not implement
Claude Code's stdio/controller protocol or its built-in tools.

```sh
bbr test //agentplane/native/claude/x/custom_sampler:test_sampler \
  //agentplane/native/claude/x/custom_sampler:sampler \
  //agentplane/native/claude/x/custom_sampler:executor
```

The test starts the pinned CLIProxyAPI server with a fake API key, a loopback Anthropic upstream
and `fingerprint-profile: claude-code-cli`. The downstream sampler sends ordinary Messages
requests. The profile is an explicit proxy configuration choice, not a copied CLI identity.
Captures under undeclared outputs contain provider requests, selected identity headers and
process logs; authentication headers are excluded.

`executor_bin --workspace PATH` prints its loopback `/append` URL. It requires an existing
workspace. `sampler_bin` takes `--messages-url`, `--executor-url`, `--model`,
`--credential-env`, `--max-requests` and one prompt. Build/run these with the repository's
`bb` commands; keep the model credential only in the sampler's environment. The integration
fixture launches the executor with an explicit environment containing no model credential.

This is an in-memory, single-use sampler. Provider failures, exhausted request budgets and
cancellation propagate without retries. An executor effect can precede cancellation or loss
of its receipt; audit the workspace before starting a new sampler. There is no journal,
restart recovery, deduplication, fencing, executor cancellation protocol, compaction, instruction
discovery or multi-turn user control. The loopback executor has no authentication and its
resolved-path check does not defend against concurrent symlink replacement. It is fixture
code, not an exposed service or a security boundary.

The [investigation report](../../../../debug/claude_custom_sampler.md) records the observed
proxy behavior and the limits of provider/subscription compatibility evidence.
