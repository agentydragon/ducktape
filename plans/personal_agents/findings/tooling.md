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
