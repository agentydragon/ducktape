# In-cluster loom/gym eval

On-demand Job that runs the forecasting eval against the **docker-ci** DinD over
mTLS, with the sandbox containers reaching the wayback cache via its in-cluster
ClusterIP. The daemon target, upstream, env, args, and image staging all live in
`eval-job.yaml` (commented) — `claude-sandbox` is `baseline` PSA (no privileged →
no local DinD), hence the remote daemon. This README is just the run procedure.

Prereqs: `docker-ci` Running on OVH; the `ghcr.io/agentydragon/{loom-gym-eval,wayback-proxy,loom-gym-sandbox}`
images published (on merge to `devel`); the mitmproxy `ignore_hosts` passthrough
reconciled; and the reflected `litellm-master-key` + `claude-forgejo-credentials`
secrets present in `claude-sandbox`.

## 1. Create the docker-ci mTLS secret (the only non-manifest step)

Public certs are in-repo; the client key is SOPS-encrypted (claude-web can decrypt).
From the repo root in the devshell:

```bash
sops -d secrets/docker-ci/client-key.sops.pem > /tmp/dc-key.pem
kubectl -n claude-sandbox create secret generic docker-ci-client \
  --from-file=ca.pem=cluster/k8s/docker-ci/certs/ca.pem \
  --from-file=cert.pem=cluster/k8s/docker-ci/certs/client-cert.pem \
  --from-file=key.pem=/tmp/dc-key.pem \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/dc-key.pem
```

## 2. Run and fetch results

```bash
kubectl apply -f loom/gym/k8s/eval-job.yaml
kubectl -n claude-sandbox logs -f job/loom-gym-eval

POD=$(kubectl -n claude-sandbox get pod -l job-name=loom-gym-eval -o name)
kubectl -n claude-sandbox cp "${POD#pod/}:/work/logs" ./eval-logs
```

Tune model / task-filter / arms via `args` in `eval-job.yaml`.
