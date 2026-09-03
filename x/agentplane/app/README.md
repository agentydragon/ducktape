# Agentplane integration app

The browser and agent surface over Agentplane's sandboxes: a FastAPI service that stamps
Sandboxes from the namespace's `SandboxTemplate`, dials each runner Pod over the runner protocol,
streams sessions to the browser over SSE, and copies every event into the trajectory store as it
arrives. The staging instance lives in `cluster/k8s/agentplane-staging/`.

```sh
bbr test //x/agentplane/app/...
```

## Layout

- `main.py`: the entry point; `Settings` names every knob as a flag, an `AGENTPLANE_*`
  variable, and a key of the YAML file `AGENTPLANE_CONFIG_FILE` points at.
- `inventory.py`: the sandbox inventory read from and written to Kubernetes (create, suspend,
  resume, archive, delete), with the parsed subset of each CR it needs.
- `bridge.py`: one runner attachment per streaming session, fanned out to every browser tab, and
  the SSE framing; `api.py` is the REST surface and the OpenAPI schema `export_schema.py` emits
  for the frontend's generated client.
- `trajectory.py`: the PostgreSQL store of threads and their events.
- `frontend/`: the React SPA on the repo's `ts_library` and esbuild toolchain, with the visual
  scenarios under `frontend/visual/`.

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

Kubernetes is the sandbox inventory, including the archived flag; the runner holds the live
session; PostgreSQL holds the copy of every event that outlives the sandbox. The app stores no
product state beyond that until a feature needs it.

## Decisions

- **Connection direction:** the app dials the runner Pod's address directly, re-resolving on
  reconnect; Pod replacement changes the address and the session log makes the cursor valid across
  it. A Service per sandbox is not needed until something outside the cluster must reach a runner.
- **Staging first, on the cheap key:** the first instance exists for the agent to test against
  autonomously, so its runner Pods carry the `cheap-experiments` LiteLLM key as environment, the
  same operational convenience the `agent-workspaces` Codex lane uses. The credentialless egress
  design in [the ADR](../plans/adr_sandbox_proxy_gateway.md) governs external systems, not the
  model endpoint.
- **Transport security on the runner port:** Cilium policy between the app namespace and the
  sandbox Pods is the v0 control. Authentication on the port itself waits for the credentialed
  readiness gate.
- **No gRPC-Web or Connect proxy in front of the runners:** browsers cannot carry the
  bidirectional `Attach`, and a standard proxy would still need per-sandbox routing to Pod
  addresses that change on every resume. The app stays the one HTTP surface; the schema is shared
  through proto-JSON and generated types instead. Splitting `Attach` into a server-streaming
  `Open` plus unary commands waits for a second, non-browser client that wants it, since the
  stream is what identifies the controlling attachment today.
- **One replica:** the bridge holds live runner attachments in memory, and a second replica would
  supersede them.
