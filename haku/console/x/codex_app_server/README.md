# Codex app-server runtime adapter

The Console-side launch adapter for OpenAI Codex as a CLI harness, launch-only (#4667): `config.py`
carries the deploy-owned implementation config and `runtime.py` builds the `HarnessLaunch` the
journal bridge sends the runner. It adds no deploy runtime selection, sandbox namespace,
credentials, or conversation writer of its own beyond what a registration supplies.

The app-server protocol, its projection into neutral operations, and the sanitized fixture capture
live runner-ward in <../../../runner/codex/> (`protocol.py`, `projection.py`, `harness.py`).
