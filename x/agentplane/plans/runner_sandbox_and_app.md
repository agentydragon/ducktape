# Runner in a Sandbox, and the first integration app

Status: **in progress**. The runner image, its Pod contract, the `agentplane-staging` namespace,
and the app's sandbox inventory with archive have landed; what remains is the bridge and UI in
review, the app's deployment, and the first real turn on staging. What has landed is described by
[`../runner/SPEC.md`](../runner/SPEC.md), [`../runner/README.md`](../runner/README.md), and
`cluster/k8s/agentplane-staging/`, not here.

## Shape

```text
browser (behind Authentik)
   |  REST + SSE
integration app (Deployment, namespace agentplane-staging)
   |  Kubernetes API              |  runner protocol (gRPC, in-cluster)
Sandbox -> Pod, PVC               |
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
may proceed in parallel and rebase. Packages that have landed leave this list.

### I4. First real turn, and continuity across suspension

Manual milestone, run by the agent on staging: create a sandbox, attach, run one turn on each
provider against LiteLLM, detach, suspend, resume, reattach from the cursor, and see the earlier
turn in the resumed conversation. This is the live-probe continuity check deferred from the runner
PR. Write what was observed into the runner SPEC where it changes a guarantee.

### C2. Runner bridge

The app's runner side: for one sandbox, resolve the Pod address, list sessions, open or attach
with a cursor, forward inputs, interrupts, and shutdown, and stream events to the browser over SSE
with the sequence as the SSE id so a reconnecting browser resumes from where it was. `Native`
events pass through unchanged; the raw view is a client-side filter over them and
`source_sequences` links a derived event to its frames.

The bridge introduces no second schema. SSE payloads are the proto-JSON encoding of the exact
`Event` messages in `protocol.proto`, and command bodies are proto-JSON of the client messages;
what the bridge owns is routing and framing only. The runner takes one attachment per session,
and the bridge holds it while any tab streams the session, fanning its events out: a session open
in several tabs updates in all of them, and a tab opened later loads the history first.

Tested against a local runner with the scripted model, reusing
[`../runner/testing/`](../runner/testing/); the app's tests never need a cluster for this part.

### C3. UI

A small React SPA on the repo's `ts_library` and esbuild toolchain: sandbox list with create,
suspend, resume, delete, and provisioning state; a session view with the turn and item stream, a
raw-frames toggle, an input box, and interrupt. Honest states: provisioning, running, suspended,
harness lost, input uncertain. No names, archive, or timeline product features yet; those are the
conversation-app package in the DAG.

Depends on C2's schema.

### C4. App deployment into staging

`cluster/k8s/agentplane-staging/`: Deployment, Service, ingress behind Authentik forward-auth, and
a ServiceAccount with RBAC over sandboxes, the template, and pods in the namespace only. Image registered
like the runner's. The agent reaches the API from inside the cluster, through the Service from
its sandbox or by port-forward, so the app's flows are testable end to end autonomously. A
production instance is a second copy of the same manifests with its own keys, and does not exist
until something needs it.

Depends on C2 and C3 producing the image.

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
