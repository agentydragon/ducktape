# Runner in a Sandbox, and the first integration app

Status: **next slice**. The runner protocol under [`../runner/`](../runner/) drives both harnesses
behind one contract with cursor reattach and restart recovery, proven against a scripted model.
Nothing runs it in a Pod yet, and nothing speaks it but its tests. This slice puts the runner in an
Agent Sandbox and builds the smallest app that manages those sandboxes and shows what a session did.

## Shape

```text
browser (behind Authentik)
   |  REST + SSE
integration app (Deployment, namespace agentplane-staging)
   |  Kubernetes API              |  runner protocol (gRPC, in-cluster)
SandboxClaim -> Sandbox -> Pod    |
                  runner container <-+
                    runner process, state dir on the PVC
                    claude / codex child per session
                    model traffic to LiteLLM with the cheap-experiments key from a Secret
```

Kubernetes is the sandbox inventory and the runner's session log is the history; the app persists
nothing of its own in this slice. A suspended sandbox keeps its CR and PVC, so it stays listable
and its history survives; deleting a sandbox deletes its history, and that is the v0 contract.

## Work packages

The packages are independent PRs; the dependencies named are the only real ones. Everything else
may proceed in parallel and rebase.

### I1. Runner image

`//x/agentplane/runner:image`: the runner `py_binary`, the pinned Claude Code and Codex
distributions Bazel already fetches for the tests (`@claude_code_cli_linux_x64`,
`@agentplane_codex_cli_linux_x64`), CA certificates, non-root. Registered in
`devinfra/ci/image_targets.json` for the Forgejo registry like `haku-harness-runner`, with a
`requires_docker` smoke test that starts the container, attaches over the protocol, and runs one
scripted turn on each harness. The image carries no credential and no Haku code.

Gate: the smoke test passes on RBE; the image is pushed on `devel`.

### I2. Runner pod contract

What the runner needs to be a long-lived container rather than a test subprocess:

- listen on the Pod address and a fixed port, with the state directory on the PVC;
- both providers configured from the environment: binaries baked in, LiteLLM base URLs, and the
  lane keys read from the variables `main.py` already names;
- a `ListSessions` unary RPC (session id, spec, harness state, last sequence), since session ids
  are client-chosen and the app keeps no record of its own;
- SIGTERM stops every harness through the existing stdin-close ladder, so
  `terminationGracePeriodSeconds` must exceed the ladder's total grace; a session whose harness was
  stopped that way reports `HarnessExited` with `stopped_by_runner`, not `HarnessLost`;
- a readiness signal the Pod can probe (the listener accepting connections is enough).

Depends on nothing; lands with or before I1.

### I3. Staging namespace

The first instance is staging, and it is built so the agent can test on it without Rai.
`cluster/k8s/agentplane-staging/`: the `agentplane-staging` namespace with its quota, a
`SandboxTemplate` `agentplane-runner` whose Pod runs the I1 image with a PVC for the state
directory and workspace, and Cilium policy: the Pod reaches DNS and LiteLLM; the app reaches the
runner port; nothing else in either direction. No warm pool: the app creates a `SandboxClaim` per
sandbox. Suspension is the Sandbox's `operatingMode: Suspended`, which the spike showed replaces
the Pod and keeps the PVC.

Sandbox PVCs live on wyrm2, which has the memory to spare: the claim uses `local-path-proxmox`
(region `proxmox` is wyrm2's label; `local-path-home-ssd` selects the OptiPlex and retains its
volumes, neither of which is wanted here), and the template's `nodeSelector` names the same
region so `WaitForFirstConsumer` binds the volume where the Pod runs. Its `Delete` reclaim policy
is what makes deleting a sandbox free its disk. The runner Pod is therefore pinned to one node,
and a suspended sandbox resumes only there; a zone-neutral local-path class spanning OVH and home
is a possible later change that would only alter the class name here.

Model access is the `cheap-experiments` LiteLLM key, which caps spend and allows only the cheap
models; both harnesses get it as their API key with LiteLLM as the endpoint. That key is today
handed out only through expiring Haku grants, by design; staging gets a standing copy as a second
`kubernetes_secret` in `tf/gitops/litellm-keys`, and the key's own budget and allowlist remain the
kill switch. Which models a session may name is whatever the allowlist carries.

Agent access is standing, not granted per task: the namespace is labeled agent-readable for
metadata and logs, so the Kyverno-generated bindings cover reads, and a per-service `agent-rbac/`
binding (the pattern in the agent RBAC base README) grants the existing agent identities
create and delete on claims, patch on sandboxes for suspend and resume, exec on runner Pods, and
port-forward, in this namespace only. No new ServiceAccount or token to distribute.

Can be authored before I1 publishes; the Pod comes up once the image exists.

### I4. First real turn, and continuity across suspension

Manual milestone, after I1 to I3: create a claim, attach, run one turn on each provider against
LiteLLM, detach, suspend, resume, reattach from the cursor, and see the earlier turn in the resumed
conversation. This is the live-probe continuity check deferred from the runner PR. Write what was
observed into the runner SPEC where it changes a guarantee.

### C1. Sandbox inventory

The app's Kubernetes side, clean-room (Haku Console's `sandbox_claims.py` is evidence, not a
dependency): list the claims and sandboxes labeled as Agentplane's with their provisioning state,
create a claim with the provider and model recorded as labels, suspend, resume, delete. REST with
an OpenAPI schema. Tested against a fake Kubernetes API or a disposable namespace on RBE, whichever
the first test needs.

Depends on the CR shapes, which exist today; I3 only names the template.

### C2. Runner bridge

The app's runner side: for one sandbox, resolve the Pod address, list sessions, open or attach
with a cursor, forward inputs, interrupts, and shutdown, and stream events to the browser over SSE
with the sequence as the SSE id so a reconnecting browser resumes from where it was. `Native`
events pass through unchanged; the raw view is a client-side filter over them and
`source_sequences` links a derived event to its frames.

The bridge introduces no second schema. SSE payloads are the proto-JSON encoding of the exact
`Event` messages in `protocol.proto`, and command bodies are proto-JSON of the client messages;
the browser's TypeScript types are generated from the same file with protobuf-es. What the bridge
owns is routing and framing only. Check whether Connect's Python server is usable for the unary
and server-streaming shape; if it is, the commands and the event stream come with generated
clients, and if not, plain REST with proto-JSON bodies is the same contract by hand.

Tested against a local runner with the scripted model, reusing
[`../runner/testing/`](../runner/testing/); the app's tests
never need a cluster for this part. Depends on I2's `ListSessions`.

### C3. UI

A small React SPA on the repo's `ts_library` and esbuild toolchain: sandbox list with create,
suspend, resume, delete, and provisioning state; a session view with the turn and item stream, a
raw-frames toggle, an input box, and interrupt. Honest states: provisioning, running, suspended,
harness lost, input uncertain. No names, archive, or timeline product features yet; those are the
conversation-app package in the DAG.

Depends on C1 and C2's schema.

### C5. Archive

Marking a sandbox archived removes it from the active list and suspends it, so the Pod goes and
the PVC with the session log stays; unarchiving resumes it. The flag is a label on the claim,
since Kubernetes is the inventory in this slice, and the list view hides archived sandboxes by
default. Archive is never deletion: deleting stays a separate, explicit action. When trajectory
persistence lands, the flag moves to the thread record and archiving a thread no longer needs
its sandbox to exist.

Depends on C1.

### C4. App deployment into staging

`cluster/k8s/agentplane-staging/`: Deployment, Service, ingress behind Authentik forward-auth, and
a ServiceAccount with RBAC over claims, sandboxes, and pods in the namespace only. Image registered
like the runner's. The agent reaches the API from inside the cluster, through the Service from
its sandbox or by port-forward, so the app's flows are testable end to end autonomously. A
production instance is a second copy of the same manifests with its own keys, and does not exist
until something needs it.

Depends on C1 and C2 producing an image; the manifests can be authored earlier.

## Decisions taken here

- **Connection direction:** the app dials the runner Pod's address directly, re-resolving on
  reconnect; Pod replacement changes the address and the session log makes the cursor valid across
  it. A Service per sandbox is not needed until something outside the cluster must reach a runner.
- **Staging first, on the cheap key:** the first instance exists for the agent to test against
  autonomously, so it runs on the `cheap-experiments` LiteLLM key, mounted into the runner
  container as environment, the same operational convenience the `agent-workspaces` Codex lane
  uses. The credentialless egress design in [the ADR](adr_sandbox_proxy_gateway.md) governs
  external systems, not the model endpoint.
- **No app persistence in this slice:** Kubernetes holds the inventory, including the archived
  flag, and the runner holds the history. Sandboxes are disposable and trajectories are not, so persistence is planned rather
  than avoided: it enters with the trajectory-persistence node in [the DAG](task_dag.md), which
  needs only the runner bridge and can start alongside the UI and deployment.
- **Transport security on the runner port:** Cilium policy between the app namespace and the
  sandbox Pods is the v0 control. Authentication on the port itself waits for the credentialed
  readiness gate.
- **No gRPC-Web or Connect proxy in front of the runners:** browsers cannot carry the
  bidirectional `Attach`, and a standard proxy would still need per-sandbox routing to Pod
  addresses that change on every resume. The app stays the one HTTP surface; the schema is shared
  through proto-JSON and generated types instead. Splitting `Attach` into a server-streaming
  `Open` plus unary commands waits for a second, non-browser client that wants it, since the
  stream is what identifies the controlling attachment today.

## Left out on purpose

Trajectory persistence, named threads, and search; timeline presentation (the conversation
app); read-only follower attachments; log compaction; approvals and external access;
multiple runners per sandbox; warm pools. Each has a node in [the DAG](task_dag.md) with what it
waits on.
