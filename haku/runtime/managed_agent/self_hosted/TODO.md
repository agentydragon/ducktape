# Managed Agents — self-hosted worker (Runtime B) TODO

Built end-to-end and its control plane is provisioned (environment
`env_015uqL9WAMSDytQEWWmLG9zF`, agent, vault + `tana-mcp-ro` credential, scheduled
deployment `depl_011DSrUoXuhoDWJoPyDuePqR`; full IDs recorded on #2438). PR #2442
adds the worker image build+push and the k8s manifests
(`cluster/k8s/haku/agent-worker/`, shipped **suspended**). What remains is
operator activation (runbook in <README.md>):

- **Generate the environment key** in the Console (Environments →
  `haku-selfhosted` → "Generate environment key") and `sops`
  `cluster/k8s/haku/agent-worker/environment-key.sops.yaml` to the real value
  (placeholder today, encrypted to the cluster/Flux age key).
- **Activate + validate**: flip `suspend: false` on the Kustomization and watch
  the Deployment — first systemd-PID1 pod in the cluster, so confirm it boots
  unprivileged (cgroup-v2 delegation, writable `/run`) and tune the pod
  `securityContext` if needed.
- **Smoke test** — `ant beta:deployments run --deployment-id depl_011DSrUoXuhoDWJoPyDuePqR`,
  watch in the Console.

## Settled (not blockers)

- **Image build + push** — `nix build .#haku-worker-image` (full-NixOS,
  `nixos.nix`) → `.github/workflows/haku-worker-image.yml` imports + pushes to
  `ghcr.io/agentydragon/haku-worker`; Flux tracks the tag via the `haku-worker`
  ImagePolicy.
- **Egress** — `api.anthropic.com` is on the `haku-mitmproxy` allowlist
  (`cluster/k8s/agents/haku-mitmproxy/cnp-haku-cloud-api-egress.yaml`); the worker
  reaches the work queue through the TLS-terminating proxy and trusts its CA via
  the inject policy (imported into the systemd unit).
- **SOPS identity** — the in-cluster worker needs no `SOPS_AGE_KEY`: it uses its
  `haku-worker` ServiceAccount for `kubectl` and reads creds from k8s secrets
  (only the web home decrypts the public-`kubeapi` JWT via SOPS).
- **Git sources** — haku-state plus read-only source mirrors on the in-cluster
  Forgejo, all through the single `.netrc` written from `HAKU_GIT_HOST` +
  `haku-state-git-write`. The worker currently clones the
  `agentydragon/ducktape` mirror for `haku/base` + `haku/run.md`; the `haku`
  read grants on `agentydragon/ducktape` and `agentydragon/gaffer-private` are
  Terraform-managed in `tf/gitops/forgejo-agentydragon-repos`.
