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
