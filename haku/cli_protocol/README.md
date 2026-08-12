# haku/cli_protocol — the Claude Code CLI wire protocol

What Haku's console needs to know about the newline-delimited JSON protocol it speaks to
`claude`, and the probes that establish it.

The console drives that protocol directly rather than through the Agent SDK
(<../plans/cli_protocol_ownership.md> holds the decision), so the protocol is now something this
repo has to understand rather than depend on. Nothing here is published by Anthropic as a
contract: the surface is reverse-engineered from the bundled binary's own schemas and measured by
running it, and much of it the CLI marks `@internal`. Treat it as pinned to a CLI version.

| File          | Holds                                                              |
| ------------- | ------------------------------------------------------------------ |
| <protocol.md> | The reference: channels, frames, control requests, `initialize`    |
| <frames.py>   | Pydantic models for the slice the console acts on                  |
| <probes/>     | Runnable experiments, each printing every frame in both directions |

The client that uses all this is `haku/runtime/x/agent_sdk_transport/cli_client.py`.

## Running a probe

A probe needs a real Claude credential and makes real model calls, so none of them is a Bazel
test. Run one wherever a credential exists — a `haku-claude` sandbox pod, or any box with a
logged-in CLI:

```bash
python3 -m haku.cli_protocol.probes.initialize_fields
python3 -m haku.cli_protocol.probes.hooks
python3 -m haku.cli_protocol.probes.client_hosted_mcp
python3 -m haku.cli_protocol.probes.steering                     # mid-turn fold
python3 -m haku.cli_protocol.probes.steering interrupt           # abort leaves the queue running
python3 -m haku.cli_protocol.probes.steering interrupt cancel-queued
```

`CLAUDE_BIN` picks the binary; it defaults to `claude` on `PATH`. The SDK wheel bundles one at
`claude_agent_sdk/_bundled/claude`, which is the copy the sandbox runs and therefore the one to
probe when the question is "what will production see".

## Testing after a CLI repin

`bbr test //haku/cli_protocol:test_frames` checks the models still parse the corpus of captured
frames, which catches a renamed or retyped field but not a changed behaviour. For behaviour,
re-run the probes and reconcile <protocol.md> — the fields it calls silently-ignored or
fail-closed are the ones where a regression is invisible.
