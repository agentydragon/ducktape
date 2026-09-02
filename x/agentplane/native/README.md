# Native harness drivers

Drives the real Claude Code and Codex app-server binaries over their own stdio protocols:
`process.py` owns the pipes and records every frame as JSONL; `claude/` and `codex/` hold the
provider's wire models (`wire.py`, with the Anthropic content blocks in `claude/blocks.py`),
frame constructors (`driver.py`), step functions (send input, await a result, steer, interrupt,
resume), and the launch `command()`/`environment()` both consumers share. There is no
provider-neutral facade here.

The wire models describe only the frames a consumer reads, as observed from the pinned builds; a
frame, event, or item of a kind they do not describe decodes to a named `Unknown*` variant rather
than failing, since the harness is the writer and a newer pin may add kinds. Inbound frames are
parsed with `wire.parse_frame`; outbound frames are the models the drivers construct, serialized at
the pipe.

Three consumers: the live-capture probe in <../capture/README.md>, which runs a scenario against a
real model; the scripted tests in <../harness_tests/README.md>, which run the same steps against a
loopback model the test controls; and the runner in <../runner/README.md>, which reuses the frame
constructors and launch configuration behind its provider-neutral protocol.

What each harness exposes on its protocol, and which of it the tests cover:
<docs/protocol_roster.md>.
