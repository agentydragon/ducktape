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
The first break is **disk**: the 16 Bazel-built service images are 3.4 GiB compressed across 46 layers,
almost none shared (each image carries its own Python runtime), and containerd needs about 1 GiB of the
guest's 9 GiB free disk per image. Import dies on the 10th image with `no space left on device`.

Not yet reached, so unmeasured: the app, runner, sandbox and runner-vm images (Nix-built or restricted
visibility, not in the tarball set), Service port≠targetPort under headless Services (egress is 80→8082),
Dex/OIDC public hostnames, real credentials for the LLM path, and memory for about 13 Pods in 9.7 GB.
