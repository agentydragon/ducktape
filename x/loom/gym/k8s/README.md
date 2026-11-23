# In-cluster x/loom/gym eval

On-demand Job that runs the forecasting eval against the **docker-ci** DinD over
mTLS, with the sandbox containers reaching the wayback cache via its in-cluster
ClusterIP. The daemon target, upstream, env, args, and image staging all live in
`eval-job.yaml` (commented) — `claude-sandbox` is `baseline` PSA (no privileged →
no local DinD), hence the remote daemon. This README is just the run procedure.

The Job reaches docker-ci by its in-cluster service name (`docker-ci.docker-ci.svc`),
which `NO_PROXY` excludes — so it connects direct, bypassing the agent mitmproxy
entirely. The docker-ci server cert carries that svc name as a SAN (cert-manager
`cluster-internal-ca`; see `cluster/parked/docker-ci/README.md`).

The `docker-ci-client` mTLS Secret is issued straight into `claude-sandbox` by
cert-manager (`cluster/parked/docker-ci/certificates.yaml`) and mounted by the
Job — no manual create-secret step. The client private key never leaves the cluster.

**The cluster eval is parked.** To run it again, restore `docker-ci` on OVH and
[restore the Wayback cache](../../wayback/cache/README.md#publish-the-image-for-revival). The Job requires the
cert-manager `docker-ci-client` Secret and the reflected `litellm-master-key`
and `claude-forgejo-credentials` Secrets in `claude-sandbox`.

## Publish images for a cluster eval

The Job pulls three GHCR `:latest` tags. On revival, use a reviewed revision and
authenticate to GHCR with package write access before building and publishing the
images. The proxy load and sandbox build also supply the bare local eval tags.

```bash
bb run //x/loom/gym:eval_load
bb run //x/loom/wayback/proxy:load
docker build --platform linux/amd64 -t loom-gym-sandbox:latest x/loom/gym/sandbox
for image in loom-gym-eval wayback-proxy loom-gym-sandbox; do
  docker tag "$image:latest" "ghcr.io/agentydragon/$image:latest"
  docker push "ghcr.io/agentydragon/$image:latest"
done
```

## Run and fetch results

```bash
kubectl apply -f x/loom/gym/k8s/eval-job.yaml
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
- Archive-service contract and parked status live in
  `../../wayback/cache/README.md`. Archive.org API behavior and signal-header
  notes live in `../../docs/archive_org_apis.md`.
