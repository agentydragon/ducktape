# Implementation reuse and prior art

Status: **focused evidence notes**.

The new implementation starts under `x/agentplane/`. Existing Haku code is behavioral evidence, not a
library dependency. The first slice should be clean-room, small, and centered on native process
launch, protocol driving, transcript capture, and replay tests.

## Existing Ducktape evidence

Read these for measured behavior and fixtures, but do not import them into Agentplane:

- [`haku/cli_protocol`](../../../haku/cli_protocol/) — Claude stream/control framing and probes;
- [`haku/runner/claude/harness.py`](../../../haku/runner/claude/harness.py) — Claude subprocess I/O;
- [`haku/runner/codex/protocol.py`](../../../haku/runner/codex/protocol.py) — Codex app-server message shapes;
- [`haku/runner/codex/harness.py`](../../../haku/runner/codex/harness.py) — Codex subprocess lifecycle;
- [`haku/runner/claude/testdata/diverse_session.jsonl`](../../../haku/runner/claude/testdata/diverse_session.jsonl)
  — scrubbed Claude message/tool examples; and
- [`haku/console/docs/harness_frame_log_v3.md`](../../../haku/console/docs/harness_frame_log_v3.md)
  — earlier exact-frame capture precedent.

Do not import `haku/runner`, `haku/cli_protocol`, or `haku/console`, and do not carry their Session,
database, projection, or recovery assumptions into the new code.

## Useful external references

- [`plotarmordev/claude-pool`](https://github.com/plotarmordev/claude-pool) — MIT-licensed Claude
  subprocess, process-group, stderr-drain, fake-CLI, and lifecycle-test patterns.
- [`openai/symphony`](https://github.com/openai/symphony) — Apache-2.0 Codex app-server handshake,
  partial-line parsing, and terminal-settlement examples.
- [`backnotprop/plannotator`](https://github.com/backnotprop/plannotator) — permissively licensed
  Codex transport/parser patterns.
- [`kubernetes-sigs/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox) — use as the
  workload backend rather than recreating Sandbox/Pod/PVC lifecycle.

Pin exact revisions in implementation comments or a later dependency note only if code is actually
borrowed. A URL and a short behavioral note are enough for this experiment.

## Borrow and adapt

Appropriate patterns to reuse conceptually:

- launch one native child with dedicated stdin/stdout/stderr pipes;
- continuously drain stderr;
- isolate the child in a process group so the test can stop it without stopping the driver;
- parse newline-delimited records across arbitrary pipe-read boundaries;
- route correlated JSON-RPC responses for Codex; and
- use fake binaries or fake model servers for deterministic tests.

Adapt these to preserve the complete native transcript during a live capture while keeping Claude
and Codex scenario logic separate. Probe logs stay outside Git; the scripted tests under
[`../harness_tests/`](../harness_tests/) carry the behavioral contract. Do not hide
provider behavior behind a common state machine before the capture tests show that the behavior is
shared.

## Do not copy

- PTY, tmux, pane scraping, or prompt-detection integration;
- automatic prompt retry after uncertain native process death;
- a process-lifetime session id presented as durable provider resume;
- a sequential JSON-RPC waiter that cannot handle concurrent messages; or
- a large artifact/promotion, checksum, DLP, persistence, or fencing framework.

## Agent Sandbox boundary

Agent Sandbox is the preferred first provisioning backend. Reuse its Claim -> Sandbox -> Pod/PVC
lifecycle rather than implementing a parallel workload controller. The native capture slice may run
locally or in a disposable Sandbox, but it does not need to own Kubernetes reconciliation.

The upstream documentation supports composing a separate Kubernetes Service around Sandbox ports;
the pinned local manifests do not prove that the Sandbox CR creates that Service. Keep Service
selection and control-channel security out of the capture implementation.

## Later, only if the product needs it

After useful Claude and Codex captures exist, evaluate whether to add:

- a small provider-neutral bridge interface;
- durable product Thread/Input/Turn records;
- central recovery decisions and explicit uncertain outcomes;
- reconnect handling; or
- a UI projection.

Do not implement those as speculative prerequisites. The first test that cannot pass without one is
the right place to introduce it.
