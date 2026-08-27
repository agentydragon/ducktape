# haku/cli_protocol — the Claude Code CLI wire protocol

What Haku's console needs to know about the newline-delimited JSON protocol it speaks to
`claude`, and the probes that establish it.

The console drives that protocol directly rather than through the Agent SDK (decided 2026-08;
the why lives in the owning modules' docstrings, and the remaining work — npm sourcing, unadopted
handshake capabilities — in <../plans/cli_protocol_ownership.md>), so the protocol is now
something this repo has to understand rather than depend on. Nothing here is published by
Anthropic as a contract: the surface is reverse-engineered from the bundled binary's own schemas
and measured by running it, and much of it the CLI marks `@internal`. Treat it as pinned to a
CLI version.

| File                                     | Holds                                                              |
| ---------------------------------------- | ------------------------------------------------------------------ |
| <protocol.md>                            | The reference: channels, frames, control requests, `initialize`    |
| <frames.py>                              | Pydantic models for the slice the console acts on                  |
| [`frame_identity.py`](frame_identity.py) | Which wire field identifies each frame, for replay dedupe          |
| <probes/>                                | Runnable experiments, each printing every frame in both directions |
| <testdata/>                              | One scrubbed capture of a real session, kept as evidence           |

The Claude provider client that uses all this is `haku/console/x/claude_code/client.py`.

## Running a probe

A probe needs a real Claude credential and makes real model calls, so none of them is a Bazel
test. Run one wherever a credential exists — a `haku-claude` sandbox pod, or any box with a
logged-in CLI. **The calls are billed to whichever account that CLI is logged in as** and come
out of its rate-limit window; the suite is a few dollars and a dozen turns, which is worth
knowing before re-running it on a laptop that happens to be logged in as you.

```bash
python3 -m haku.cli_protocol.probes.initialize_fields
python3 -m haku.cli_protocol.probes.hooks
python3 -m haku.cli_protocol.probes.client_hosted_mcp
python3 -m haku.cli_protocol.probes.steering                     # mid-turn fold
python3 -m haku.cli_protocol.probes.steering interrupt           # abort leaves the queue running
python3 -m haku.cli_protocol.probes.steering interrupt cancel-queued
python3 -m haku.cli_protocol.probes.compaction /tmp/capture.jsonl  # hooks + an in-process tool + a forced compaction
```

Run each from a **throwaway working directory**: the CLI reads the cwd's `CLAUDE.md` and settings,
and a probe run inside this repo measures this repo's configuration rather than the protocol.

`CLAUDE_BIN` picks the binary; it defaults to `claude` on `PATH`. The SDK wheel bundles one at
`claude_agent_sdk/_bundled/claude`, which is the copy the sandbox runs and therefore the one to
probe when the question is "what will production see".

## Committing a capture

`probes/compaction.py` is the one probe that leaves a file behind. Everything it writes is a real
session on the machine that ran it, so it is scrubbed and then **checked structurally** before it
goes anywhere:

```bash
python3 -m haku.cli_protocol.probes.redact_capture /tmp/capture.jsonl testdata/compaction_session.jsonl
```

That renumbers every UUID, rewrites paths to `/workspace`, elides the operator's skill/agent/MCP/
plugin catalogs and the opaque `signature` on every `thinking` block, then refuses to finish while
any long opaque token, absolute path, email, credential or this machine's own identity survives. It
exits non-zero rather than writing something unsafe. Grepping for the obvious is what missed the
thinking signatures last time; the shape check is what catches them.

<test_compaction_capture.py> reads the result, so a CLI repin that changes how compaction reaches
the wire fails a test rather than going unnoticed.

## Testing after a CLI repin

`bbr test //haku/cli_protocol:...` runs checks of differing reach. `test_frames` checks the models
still parse the corpus of captured frames, which catches a renamed or retyped field.
`test_compaction_capture` checks the captured session still holds the properties a reader relies on
across a compaction — that nothing is retracted, that the summary arrives as a synthetic `user`
frame, that `PreCompact` runs in time to matter. `test_frame_identity` checks each frame class still
carries the field replay dedupes on. None of them catches a changed behaviour, because all read a
recording. For behaviour, re-run the probes and reconcile <protocol.md> — the fields it calls
silently-ignored or fail-closed are the ones where a regression is invisible.
