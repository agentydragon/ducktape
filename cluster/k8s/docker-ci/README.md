# Docker CI (DinD)

Persistent Docker-in-Docker daemon for CI container tests. Caches image layers
across test runs, avoiding the ~46s cold-pull penalty on disposable RBE executors.

## Architecture

The daemon listens with `--tlsverify`. There are two would-be clients; only the
in-cluster one is wired up today:

```text
in-cluster eval Job (loom/gym, claude-sandbox ns) — LIVE
  │  mounts the cert-manager Secret docker-ci-client (ca/cert/key)
  │  DOCKER_HOST=tcp://docker-ci.docker-ci.svc.cluster.local:2376
  ▼
DinD pod (k8s, OVH) ──── mTLS ──── external RBE via TLSRoute — DORMANT
  ▲                                  docker-ci.allegedly.works:2376
  │                                  (DUCKTAPE_DOCKER_CLIENT_KEY export is
  └── --tlscacert = cluster root      commented out; docker_mtls fixture no-ops)
```

## mTLS Certificate Setup

cert-manager issues the whole PKI from the cluster-internal CA — there is **no
hand-rolled CA and no SOPS key material**. Both leaves are ECDSA P-256, chain to
`cluster-root-ca`, and auto-rotate. Defined in `certificates.yaml`:

| Certificate        | Namespace        | SANs / usage                                                                     | Secret                 |
| ------------------ | ---------------- | -------------------------------------------------------------------------------- | ---------------------- |
| `docker-ci-server` | `docker-ci`      | `docker-ci.allegedly.works`, `docker-ci.docker-ci.svc.cluster.local`; serverAuth | `docker-ci-server-tls` |
| `docker-ci-client` | `claude-sandbox` | CN `docker-ci-client`; clientAuth                                                | `docker-ci-client`     |

Both Secrets carry the standard cert-manager keys (`tls.crt`, `tls.key`,
`ca.crt`, where `ca.crt` is the cluster root). The Deployment and the eval Job
project these to the `ca.pem`/`*-cert.pem`/`*-key.pem` names the docker daemon
and CLI expect (`spec.volumes[].secret.items`).

Because both leaves chain to `cluster-root-ca`, the daemon's `--tlscacert`
(`ca.crt`) verifies the client and the client's `ca.pem` verifies the server —
no separate client CA needed.

### Rotation

Automatic. cert-manager renews each leaf 30 days before its 90-day expiry. The
on-demand eval Job mounts the current client Secret on each run; the long-lived
DinD pod (which does not hot-reload TLS) is bounced by the stakater reloader via
the `reloader.stakater.com/auto: "true"` annotation on its Deployment.

**Adding a SAN** (the original reason this PKI was hand-rolled): edit
`dnsNames` on the `docker-ci-server` Certificate. No admin key, no re-signing.

The external-RBE client path (`bbr test` over `DUCKTAPE_DOCKER_CLIENT_KEY` + the
`util/testing/docker_mtls.py` fixture) is **dormant** — see that fixture's
docstring and the tombstone in `devinfra/secrets/_common.sh` for how to revive it.

## Kubernetes Resources

- **Namespace**: `docker-ci` (privileged PSA — DinD requires it)
- **Deployment**: `docker:27-dind` with `--tlsverify`, scheduled on OVH workers
  (`topology.kubernetes.io/region: hil`); `reloader.stakater.com/auto` for cert rotation
- **Storage**: `emptyDir` (30Gi) for `/var/lib/docker` — a disposable overlay2
  cache, hence no PVC (rationale in `deployment.yaml`).
- **Service**: ClusterIP on port 2376
- **Certificates**: `docker-ci-server` (docker-ci ns), `docker-ci-client` (claude-sandbox ns)

## Maintenance CronJobs

- **Container prune**: hourly, removes stopped containers older than 1h
- **Image prune**: weekly (Sunday 3am), removes unused images older than 7 days

Both use `kubectl exec` into the DinD pod via a dedicated ServiceAccount.
