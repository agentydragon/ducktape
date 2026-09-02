# Native harness drivers

Drives the real Claude Code and Codex app-server binaries over their own stdio protocols:
`process.py` owns the pipes and records every frame as JSONL; `claude/` and `codex/` hold the
provider's frame constructors, step functions (send input, await a result, steer, interrupt,
resume), and the launch `command()`/`environment()` both consumers share. There is no
provider-neutral facade here.

Two consumers: the live-capture probe in <../capture/README.md>, which runs a scenario against a
real model, and the scripted tests in <../harness_tests/README.md>, which run the same steps against
a loopback model the test controls.

What each harness exposes on its protocol, and which of it the tests cover:
<docs/protocol_roster.md>.
