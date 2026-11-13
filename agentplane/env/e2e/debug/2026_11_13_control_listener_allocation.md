# Runtime control-listener allocation

Tracker: [#16231](https://github.com/agentydragon/ducktape/issues/16231).

Required CI for source `135dba720768d9dfdb60a122145d9d23f9699dd0`
failed during the first `test_codex_shared_process` case's setup. The production
Runtime container exited with:

```text
listen for Gateway control: listen tcp 127.0.0.1:43377: bind: address already in use
```

The fixture selected an ephemeral port by binding `127.0.0.1:0`, read the port,
then closed that socket before the Runtime container started. That leaves the
address available for reuse during intervening service setup. The evidence
identifies the allocation gap; it does not identify the process occupying 43377.
The failed test action is in
[invocation 03a1aa59](https://app.buildbuddy.io/invocation/03a1aa59-ade3-5637-97da-de0d6779f80c).

The direct control-listener fixture now derives a loopback address from the
case's fresh Sandbox UID and uses port 9090 on that address. This keeps it outside
the default-loopback ephemeral allocation that caused the observed collision.
It does not reserve a socket, retry startup, or change the production Runtime.
App and Sampler fixtures that explicitly supply the production Gateway endpoint
continue to use that endpoint and its certificate authority.

The existing Codex shared-process target holds a listening socket on
`127.0.0.1` at the fixture's chosen port before starting the actual Runtime.
Its four cases retain their Session, shared-process, restart and cleanup
assertions. The old allocator must fail actual Runtime startup under that same
competition; the new address must let the cases complete. Compare remote source
digests as well as results: `bbr --run_from_commit` intentionally excludes
uncommitted patches and cannot validate a working-tree repair.

Follow-up: after the repair lands, record seven days of fresh physical test
actions and recurrence signatures in #16231 and
[#16034](https://github.com/agentydragon/ducktape/issues/16034). Cached successes
are not new failure exposure. A deterministic regression pass establishes this
mechanism's repair, not a population flake-rate or complete-feedback saving.
