# In-cluster loom/gym eval

Run the forecasting eval as an on-demand Kubernetes Job in `claude-sandbox`. The
driver orchestrates Inspect-AI docker-compose sandboxes on the **docker-ci** DinD
over mTLS; the contestant + wayback-proxy containers run on that daemon and reach
the wayback cache by its in-cluster ClusterIP (no public gateway → no Envoy 60s
cap). Applied by hand (not Flux) — a Job runs once and is not continuously
reconciled.

## Why a remote daemon (not DinD in claude-sandbox)

`claude-sandbox` enforces `baseline` PodSecurity, which forbids `privileged`, so a
DinD sidecar can't run here. `docker-ci` is the cluster's privileged-PSA home for
exactly this. The driver reaches it by its **public name**
(`docker-ci.allegedly.works:2376`, matching the cert SAN) through the agent
mitmproxy, which raw-tunnels that host (`ignore_hosts`, see
`cluster/k8s/agents/mitmproxy/deployment.yaml`) so docker mTLS passes end-to-end.

## Prerequisites (per cluster, mostly one-time)

1. **`docker-ci` up on OVH** — `kubectl -n docker-ci get pod` shows `Running`.
2. **Images in GHCR** (all under `ghcr.io/agentydragon/`, published on merge to
   `devel`): `loom-gym-eval` and `wayback-proxy` (oci_image matrix rows in
   `push-images.yml`), and `loom-gym-sandbox` (Dockerfile job in
   `container-images.yml` — it's an interactive python env, not a `py_binary`).
3. **mitmproxy passthrough reconciled** — the `ignore_hosts` + egress rule for
   `docker-ci.allegedly.works:2376` are live (merged + Flux-reconciled).
4. **Reflected secrets present** in `claude-sandbox`: `litellm-master-key`,
   `claude-forgejo-credentials` (the latter feeds `ensure_checkout`).

## 1. Create the docker-ci mTLS client secret

The public certs are in-repo; the client key is SOPS-encrypted (claude-web can
decrypt). From the repo root, in the devshell (`SOPS_AGE_KEY` set):

```bash
sops -d secrets/docker-ci/client-key.sops.pem > /tmp/dc-key.pem
kubectl -n claude-sandbox create secret generic docker-ci-client \
  --from-file=ca.pem=cluster/k8s/docker-ci/certs/ca.pem \
  --from-file=cert.pem=cluster/k8s/docker-ci/certs/client-cert.pem \
  --from-file=key.pem=/tmp/dc-key.pem \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/dc-key.pem
```

## 2. Run the eval

```bash
kubectl apply -f loom/gym/k8s/eval-job.yaml
kubectl -n claude-sandbox logs -f job/loom-gym-eval
```

The Job's `stage-images` initContainer first pulls `wayback-proxy` and
`loom-gym-sandbox` from GHCR (`:latest`) and tags them into the docker-ci daemon
under the bare `:latest` names the compose expects (`x-local` — no pull at
sandbox-create). Then the `eval` container runs. Edit `args` in `eval-job.yaml`
for model / task-filter / arms (defaults: `glm-4.5`, `manifold-`, archive arm,
in-cluster cache upstream).

## 3. Fetch results

`.eval` logs are written to `/work/logs` on the pod's emptyDir. Before the Job's
`ttlSecondsAfterFinished` reaps it:

```bash
POD=$(kubectl -n claude-sandbox get pod -l job-name=loom-gym-eval -o name)
kubectl -n claude-sandbox cp "${POD#pod/}:/work/logs" ./eval-logs
```
