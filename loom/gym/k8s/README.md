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

## Operational Gotchas

- Delete the prior Job before re-applying. A run is not idempotent and
  `backoffLimit: 0` is intentional.
- Inspect prints the score summary at the end; watch the `eval` container log
  during the run.
- `kubectl exec` and `port-forward` can fail through the kube-api MITM streaming
  path. Prefer throwaway probe pods plus `kubectl logs` for in-cluster curl or
  docker API checks.
- The shared `docker-ci` daemon has a finite network pool. A killed eval can
  leave orphaned compose containers/networks; keep `--max-samples 8` unless the
  daemon is dedicated to this eval, and clean stale `inspect-*` containers plus
  `docker network prune` through the docker API if the pool is exhausted.
- IA/archive failures often surface as no-answer samples: exhausted agent loop,
  empty answer, `JSONDecodeError`, and `value=nan`. Treat the `nan` count as an
  archive reliability signal before assuming a model or scoring bug.
- Archive-service status, first cold-run results, and limiter follow-ups live in
  `../../wayback/cache/PLAN.md`. Archive.org API behavior and signal-header
  notes live in `../../docs/archive_org_apis.md`.
