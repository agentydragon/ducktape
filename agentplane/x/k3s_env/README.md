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

Every Deployment of the stack except Dex (13, the app included) becomes Available on one RBE action (about 8.5 minutes with the images
already in the cache), with the changes below. Breaks found on the way, in order:

- **Kernel sysctls:** the kubelet needs every `/proc/sys/kernel` entry present in the shadow copy, not only
  the keys ones.
- **Disk:** the 16 Bazel-built service images are 3.4 GiB compressed across 46 layers, almost none shared
  (`py_image_layer` puts the interpreter under each binary's own `.runfiles` tree), and containerd needs about
  1 GiB per image. The default 12 GiB disk fails on the 10th image; `EstimatedFreeDiskBytes=50GB` fixes it.
- **No ClusterIP:** without kube-proxy, Pods reach neither the `kube-dns` Service nor `kubernetes.default`.
  CoreDNS runs with `hostNetwork` on the node IP, the kubelet's `cluster-dns` points at it, and every
  container gets `KUBERNETES_SERVICE_HOST`/`PORT` for the node IP.
- **KubeVirt kinds:** the sandbox controller's cache needs `VirtualMachine`/`VirtualMachineInstance` to exist, so
  schemaless stub CRDs are applied. It should instead start without KubeVirt when the kinds are absent.
- **CNPG's `postgres-app` Secret:** the app reads its database credentials from the Secret CNPG generates for
  its `app` owner; the plain Postgres stand-in gets an equivalent.
- **Dex dropped:** its config reaches the Pod through an ExternalSecret template (`regexReplaceAll`, `htpasswd`
  are External Secrets functions). `render.py` does not evaluate those, and the image's gomplate rejects them.
  Dex returns once the emulation renders that template (it needs a bcrypt hash).

Not yet reached: the app, runner, sandbox and runner-vm images (Nix-built or restricted visibility, not in the
tarball set), Service port≠targetPort under headless Services (egress is 80→8082), Dex/OIDC public hostnames,
real credentials for the LLM path, and any test traffic: the Deployments are Available but nothing has
exercised them.
