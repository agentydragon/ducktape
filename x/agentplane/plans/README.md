# Agentplane

Status: **focused proposal**. “Agentplane” is the deliberately boring working name for the native
harness bridge/controller; it is not a Haku implementation refactor.

Agentplane runs native Claude Code and Codex harnesses in replaceable Kubernetes workloads and
speaks their structured machine protocols. The first slice is in place: native drivers for both
harnesses ([`../native/`](../native/)), a live-capture probe ([`../capture/`](../capture/)),
scripted behavioral tests against a loopback model ([`../harness_tests/`](../harness_tests/)), and
the runner that serves both harnesses behind one gRPC contract ([`../runner/`](../runner/)).

## Documents

- [Focused architecture](architecture.md)
- [Claude and Codex protocol notes](provider_protocols.md)
- [Implementation reuse and prior art](implementation_reuse.md)
- [A2A suitability evaluation](a2a.md)
- [Focused experiments](experiments.md)
- [Agentplane task DAG](task_dag.md)
- [User stories: the shape Agentplane is growing toward](user_stories.md)
- [Runner in a Sandbox, and the first integration app](runner_sandbox_and_app.md)
- [Asynchronous approvals and notification delivery](async_approvals.md)
- [Agent access to external systems](external_access.md)
- [Product-surface inventory](product_surface.md)
- [Sandbox egress identity option survey](sandbox_egress_identity_research.md)
- [ADR: credentialless Sandbox egress](adr_sandbox_proxy_gateway.md)
- [Sandbox proxy and identity spike](../sandbox-spike/README.md)
- [Native driver README](../native/README.md)
- [Live capture probe README](../capture/README.md)
- [Scripted harness tests README](../harness_tests/README.md)
- [Runner README](../runner/README.md) and [runner SPEC](../runner/SPEC.md)
- [Common protocol: what the seam owns and the vocabulary above it](../docs/common_protocol.md)

## v0 scope

Required:

- native Claude Code and Codex drivers;
- real stdin/stdout protocol traffic, never PTY/tmux integration;
- messages, tool calls/results, streaming output, interrupts, and steering where supported;
- provider-native resume after an idle process restart where supported;
- upstream LLM request bodies and streamed response capture;
- scripted loopback-model tests through real harnesses; and
- small, hand-authored behavior assertions with synthetic workspaces.

Not required for the capture slice:

- PostgreSQL or a common Thread/Turn/Input schema;
- neutral operation projection or UI timeline;
- Kubernetes reconciliation or Service management;
- runtime-generation/fencing identities;
- artifact promotion, checksum manifests, custom DLP scanning, or package-integrity metadata;
- credentials, OAuth, approvals, MCP routing, subscriptions, or external-event adapters.

## Design boundaries

- Kubernetes/Agent Sandbox owns Sandbox, Pod, PVC, readiness, suspension, and workload
  lifecycle.
- Native harnesses own native history, execution semantics, and native resume.
- The bridge owns native process supervision and protocol I/O.
- A future central service may own product interaction state and a user-facing timeline while
  consuming those observations.

Use natural Pod UID and process start/exit evidence first. Do not require `restartPolicy: Never`,
mutual TLS, or a separately injected runtime-generation identity for v0. A separately managed
Kubernetes Service may be useful for a central-initiated channel, but the Sandbox CR is not assumed
to create one automatically.

## Evidence standard

Live probe runs preserve complete ordered native frames and the upstream model bodies/chunks. They
omit HTTP headers, cookies, environment variables, credentials, and private user data, and stay
outside Git; the scripted tests commit no recordings. Use ordinary repository secret checks and a
small obvious-token guard rather than building a promotion or DLP subsystem.

The native transcript and model transcript are the evidence. Do not add redundant body lengths,
SHA fields, timestamps, parsed copies, checksum files, or manifest inventories. File order supplies
ordering; provider-native ids remain in the provider transcript.

The common protocol is the runner; the central persistence model stays deferred until a feature of
the integration app cannot work from Kubernetes and the runner's session logs alone.
