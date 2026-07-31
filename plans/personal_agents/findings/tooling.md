# Tooling and local testing

Findings are numbered in discovery order across the whole programme and cited by
number from cluster manifests, so the IDs are stable and non-contiguous here.
Index of all findings: [README.md](README.md).

## F12. Docker-in-Kubernetes is the viable path for OpenClaw-under-OpenShell — and k3d gives us a local rig

Two experiments off the back of F11: can a Docker-driver OpenShell run in
Kubernetes, and can a local cluster replace the temporary production RBAC.

**The gateway has a Docker driver, and it selects.** `openshell-gateway --help`:

```text
--drivers <DRIVERS>   [env: OPENSHELL_DRIVERS=]
  Accepts a comma-delimited list such as `kubernetes` or `kubernetes,podman`.
  When unset, the gateway auto-detects the driver based on the runtime
  environment (Kubernetes → Podman → Docker).
```

That auto-detection order is why our in-cluster gateway is on the Kubernetes
driver, which is the one that discards the entrypoint (F11). Ran the stock gateway
image against a local Docker socket with `OPENSHELL_DRIVERS=docker`:

```text
INFO openshell_server: Using compute driver driver=docker
INFO openshell_server: Server listening address=0.0.0.0:8080
```

Two gotchas getting there, both silent-ish:

- The image runs as uid `1000:1000` and dies on `failed to create
/.local/state/openshell/gateway — Permission denied`. Needs a writable `HOME`
  or `XDG_STATE_HOME`.
- It then needs to reach `/var/run/docker.sock`, which is `root:docker` — so
  running as the image's default uid fails with a bare `Error in the hyper legacy
client: client error (Connect)` that does not mention permissions.

**So the path is: a second OpenShell gateway with `OPENSHELL_DRIVERS=docker`
alongside a Docker daemon in the pod.** That driver is the one NemoClaw uses and
the one that honours `OPENSHELL_SANDBOX_COMMAND`, so `openclaw-start` would run and
the harness would live inside the sandbox — the thing the Kubernetes driver refuses.

**Not proven end to end, and honestly so.** Creating a sandbox needs the
`openshell` CLI, which this environment cannot fetch: `openshell.ai/install.sh`
returns 502, the GitHub releases API and page return nothing through the proxy,
and guessed asset names 404. The gateway image ships only `openshell-gateway`, no
client. What is established is that the driver exists, selects, and initialises in
a container; what is not is a sandbox actually starting under it.

**Cost, before anyone gets excited.** This trades the operator's declarative model
for a privileged Docker-in-Docker pod plus a second gateway to operate. Note
`openshell-sandboxes` is already labelled
`pod-security.kubernetes.io/enforce: privileged` — OpenShell sandboxes need it —
so the precedent exists, but DinD widens it considerably. Whether such a pod is
admitted here was **not** tested: creating a privileged pod was refused by the
session's safety classifier, and working around that would have been the wrong
move.

**Local rig: `kind` does not work here, `k3d` does.** Worth knowing, because it
removes the need for time-boxed production RBAC on future experiments.

```text
kind v0.30.0 / kindest/node:v1.34.0
  INFO: detected cgroup v1
  Failed to mount cgroup at /sys/fs/cgroup/systemd: Operation not permitted
  [!!!!!!] Failed to mount API filesystems.  Exiting PID 1...

k3d v5.8.3 / k3s v1.31.5  -> cluster up in ~30s, node Ready
```

The host is cgroup v1, and kind's node image boots systemd, which cannot mount its
own hierarchy nested here. k3s does not need systemd, so k3d sidesteps it entirely.
Docker itself works fine (`29.3.1`, overlayfs, uid 0 with broad caps).

**Gotcha:** `k3d cluster create` merges into the default kubeconfig **and switches
the current context**, so the next unqualified `kubectl` hits the toy cluster
instead of production. Restore with `kubectl config use-context <prod>` and pass
`--context` explicitly, or point `KUBECONFIG` somewhere else before creating.

## F20. Langfuse receives the agent's traces; its read API falls over on modest queries

Two separate results, and it matters that they are separate — one is the
requirement, the other is the tool used to check it.

**The traces land (C4).** Per-model counts for one day, from the aggregate
endpoint:

```text
glm-5.2              traces=361     the z.ai lane (other agents)
gpt-5.6-sol          traces=25      public-coder-agent
gemini-embedding-2   traces=10      public-coder-agent memory search
gpt-oss:20b          traces=10      ollama
gpt-5.6-luna         traces=7       public-coder-agent
gpt-5.6-terra        traces=1
None                 traces=403
```

`gemini-embedding-2` is conclusive: nothing else in the cluster embeds with it,
so both the agent's chat path and its memory search reach Langfuse. A sampled
trace carried populated `input` and `output`, so the substance C6 wants is
present in principle.

**Identifying the caller is inference, not lookup.** Everything lands in one
`langfuse-litellm-project`, and `user_api_key_alias` is **not populated** on the
traces — the discriminator used above is the model name, which stops working the
moment two agents share a model. The caller identity does exist, but buried: a
JSON _string_ at `metadata.attributes.metadata` holding `user_api_key_hash`.

**The read API is the fragile part.** It materialises full request and response
bodies, and these traces carry whole `CLAUDE.md` payloads. Cost scales with
window × limit, and the web tier aborts rather than degrading:

```text
limit=5,  45 min   ->  7s
limit=20, 45 min   -> 18s
limit=25, 3 h      -> timeout, then langfuse-web exit 134 (SIGABRT, 2Gi limit)
limit=100, 8 h     -> "Empty reply from server", same abort
/api/public/metrics/daily, 1 day  -> 1.4s
```

`langfuse-web` restarted **twice** during this investigation, both times from a
read. Ingestion was unaffected throughout — that path is `langfuse-worker`, a
different pod — so observability kept working while the API used to inspect it
did not. Worth keeping the two apart when judging whether Langfuse is healthy.

**Practical rule until this is fixed:** aggregate endpoints for counts, and
`limit<=5` with a tight window when trace bodies are actually needed.
