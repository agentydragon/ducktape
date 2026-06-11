# In-cluster loom/gym eval

On-demand Job that runs the forecasting eval against the **docker-ci** DinD over
mTLS, with the sandbox containers reaching the wayback cache via its in-cluster
ClusterIP. The daemon target, upstream, env, args, and image staging all live in
`eval-job.yaml` (commented) — `claude-sandbox` is `baseline` PSA (no privileged →
no local DinD), hence the remote daemon. This README is just the run procedure.

The Job reaches docker-ci by its in-cluster service name (`docker-ci.docker-ci.svc`),
which `NO_PROXY` excludes — so it connects direct, bypassing the agent mitmproxy
entirely. The docker-ci server cert carries that svc name as a SAN (cert-manager
`cluster-internal-ca`; see `cluster/k8s/docker-ci/README.md`).

The `docker-ci-client` mTLS Secret is issued straight into `claude-sandbox` by
cert-manager (`cluster/k8s/docker-ci/certificates.yaml`) and mounted by the Job —
no manual create-secret step. The client private key never leaves the cluster.

Prereqs: `docker-ci` Running on OVH; the `ghcr.io/agentydragon/{loom-gym-eval,wayback-proxy,loom-gym-sandbox}`
images published (on merge to `devel`); the `docker-ci-client` Secret present
(cert-manager); and the reflected `litellm-master-key` + `claude-forgejo-credentials`
secrets present in `claude-sandbox`.

## Run and fetch results

```bash
kubectl apply -f loom/gym/k8s/eval-job.yaml
kubectl -n claude-sandbox logs -f job/loom-gym-eval

POD=$(kubectl -n claude-sandbox get pod -l job-name=loom-gym-eval -o name)
kubectl -n claude-sandbox cp "${POD#pod/}:/work/logs" ./eval-logs
```

Tune model / task-filter / arms via `args` in `eval-job.yaml`.
