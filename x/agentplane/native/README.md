# Native harness drivers

Drives the real Claude Code and Codex app-server binaries over their own stdio protocols:
`process.py` owns the pipes and records every frame as JSONL; `claude/` and `codex/` hold the
harness-specific wire models (`wire.py`, with the Anthropic content blocks in `claude/blocks.py`),
frame constructors (`driver.py`), capture step functions, test-only async run facades, and the
launch `command()`/`environment()` both consumers share. `claude/async_run.py` and
`codex/async_run.py` deliberately remain harness-specific: there is no harness-neutral facade.

The wire models describe only the frames a consumer reads, as observed from the pinned builds; a
frame, event, or item of a kind they do not describe decodes to a named `Unknown*` variant rather
than failing, since the harness is the writer and a newer pin may add kinds. Inbound frames are
parsed with `wire.parse_frame`; outbound frames are the models the drivers construct, serialized at
the pipe.

Three consumers: the live-capture probe in <../capture/README.md>, which runs a scenario against a
real model; the scripted tests in <../harness_tests/README.md>, which run the same steps against a
loopback model the test controls; and the runner in <../runner/README.md>, which reuses the frame
constructors and launch configuration behind its harness-neutral protocol.

What each harness exposes on its protocol, and which of it the tests cover:
<docs/protocol_roster.md>.
