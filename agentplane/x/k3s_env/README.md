# k3s environment spike

Does the rendered `agentplane-testing` stack come up on a bare k3s in an RBE action (no Cilium, no
kube-proxy)? Manual only: `bbr test //agentplane/x/k3s_env:test_env --test_output=all`.

`render.py` rewrites the generated manifest for a bare cluster: drops Cilium, Gateway API, Kyverno,
Prometheus, cert-manager, CNPG and External Secrets kinds, emulates the ExternalSecrets with random
Secrets, replaces CNPG with a single `postgres` Deployment, makes Services headless (no kube-proxy
means no ClusterIP) and generates the egress CA. `test_env.py` starts k3s (recipe from
`agentplane/x/k3s_rbe_spike`), imports the Bazel-built image tarballs, applies everything and reports
per-Deployment readiness.

## Result

k3s starts (the `/proc/sys/kernel` shadow needs every sysctl present as a file, not just the keys ones).

With the default 12 GiB disk the first break is disk: the 16 Bazel-built service images are 3.4 GiB
compressed across 46 layers, almost none shared (`py_image_layer` puts the interpreter under each
binary's own `.runfiles` tree, so every image's runtime layer differs), and containerd needs about 1 GiB
per image. `EstimatedFreeDiskBytes=50GB` on the test fixes that; all 16 import (16 GiB used).

The next break is DNS and the API: with no kube-proxy there is no ClusterIP, so Pods cannot reach the
`kube-dns` Service (every Postgres lookup fails: `failed to resolve host 'postgres-rw.agentplane-testing.svc'`)
or `kubernetes.default`. CoreDNS, local-path-provisioner and both Go controllers crash-loop, and Dex and
Electric crash-loop for reasons not yet looked at. Postgres, `llm-ingress`, `sandbox-service`, `oauth-fixture`
and `mcp-everything` run. Memory is not a problem so far (8 GiB available with 14 Pods).

Likely fix, untried: point the kubelet's `cluster-dns` at a hostNetwork CoreDNS on the node IP and inject
`KUBERNETES_SERVICE_HOST`/`PORT` (node IP, 6443) into every container.

Not yet reached, so unmeasured: the app, runner, sandbox and runner-vm images (Nix-built or restricted
visibility, not in the tarball set), Service port≠targetPort under headless Services (egress is 80→8082),
Dex/OIDC public hostnames, and real credentials for the LLM path.
