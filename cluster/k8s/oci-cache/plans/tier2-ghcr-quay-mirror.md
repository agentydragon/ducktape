# Deferred: route non-Docker-Hub pulls through oci-cache

Handoff notes. **Status as of 2026-07-05: downprioritized.** The original goal was
to keep `ghcr.io` + `pkg-containers.githubusercontent.com` out of haku-ci's egress
allowlist (now PolicySet `haku-ci-build-fetch`, which holds neither) by making
haku-ci's dind pull ghcr (and ideally quay) through the in-cluster `oci-cache` Zot
mirror — the same way Docker Hub already does (Tier 1).

That is no longer an immediate CI priority. haku-ci's only Dockerfile-built image is
the Haku-owned `haku-ui` starter copied into `haku-state`, and that Dockerfile uses
Docker Hub bases (`node:*`, `python:*`), which Tier 1 already routes through
`oci-cache`. The rest of the Haku platform images are already built outside haku-ci,
mostly by Bazel `rules_oci` in `.github/workflows/push-images.yml`; `haku-ui` itself is
being ported that way too. Until a post-port workload proves it still needs frequent
direct ghcr/quay pulls from haku-ci, keep those registry hosts on the proxy allowlist and
do not spend migration effort here. The mirror's setup (upstream prefixes, plain-HTTP
endpoint, the backend-port egress rule) is in `cluster/k8s/oci-cache/README.md`; the egress
rule's writeup is `cluster/docs/cilium_network_policy.md`.

## The blocker: classic dockerd only mirrors Docker Hub

The mirror side needs no change: on 2026-07-04 a curl pod got HTTP 200 from
`GET http://oci-cache.oci-cache.svc/v2/ghcr/project-zot/zot-minimal-linux-amd64/manifests/v2.1.11`.
The open problem is the **client**. haku-ci runs a `docker:*-dind-rootless` image
(`cluster/cdk8s/haku_ci/runner.py` pins the tag).
Tier 1 works because Docker's `--registry-mirror` handles Docker Hub — but it is
**Hub-only** and takes **no path**, which is also why `oci-cache` serves Docker Hub
at its root (a 6th Zot sync entry, destination `/`). There is **no `--registry-mirror`
equivalent for ghcr/quay** in classic dockerd.

If this ever becomes important again, per-registry (ghcr/quay) mirroring needs one of:

1. **Docker's containerd image store** (`daemon.json: {"features":{"containerd-snapshotter":true}}`)
   - containerd `hosts.toml` under `/etc/containerd/certs.d/<registry>/hosts.toml`.
     **UNVERIFIED and doubtful.** dockerd's registry-pull path may not read
     containerd's `hosts.toml` even with the containerd store; open moby issues show
     that config path is incomplete — `insecure-registries` broken
     ([moby#51080](https://github.com/moby/moby/issues/51080)) and client certs
     unsupported ([moby#51662](https://github.com/moby/moby/issues/51662)). Needs an
     empirical test (below) before trusting it.
2. **buildkit** — `buildkitd.toml` supports per-registry mirrors reliably, but
   haku-ci builds with `docker build` against the classic daemon, so this is a real
   runner migration, not a config tweak.
3. **nerdctl + containerd** — different runtime; also a migration.

## Why this was not finished

- **No access to haku-ci.** It's operator-only with **no Haku RBAC** (by design —
  see `cluster/cdk8s/haku_ci/README.md`), so an agent authenticating as `haku-k8s`
  cannot create/exec pods there.
- **haku-sandbox enforces `baseline` PodSecurity.** It's the only namespace the Haku
  agent can write to, but it **rejects `privileged` pods**:
  `violates PodSecurity "baseline:latest": privileged`. Rootless dind still needs
  `privileged: true` (per haku-ci's own comment — the non-privileged path can't
  create the rootless TAP), so a dind can't run there to test the Docker `hosts.toml`
  behaviour.

To revive this, the next agent needs **a privileged-capable namespace** (relax
haku-sandbox to `privileged`/`baseline`-warn, or use another ns), or run the test
**in haku-ci itself** with operator/admin credentials.

## The empirical test to run if revived

Stand up a pod of haku-ci's dind image (privileged; tag in `runner.py`) with the
containerd image store + `hosts.toml`, and **no `HTTP_PROXY`** so the only route to
ghcr is the mirror — a successful ghcr pull then proves `hosts.toml` routing works.

`daemon.json`:

```json
{
  "features": { "containerd-snapshotter": true },
  "insecure-registries": ["oci-cache.oci-cache.svc.cluster.local"]
}
```

`/etc/containerd/certs.d/ghcr.io/hosts.toml`:

```toml
server = "https://ghcr.io"
[host."http://oci-cache.oci-cache.svc.cluster.local/v2/ghcr"]
  capabilities = ["pull", "resolve"]
  override_path = true
```

Then `docker pull ghcr.io/project-zot/zot-minimal-linux-amd64:v2.1.11` and check
`kubectl -n oci-cache logs deploy/zot | grep ghcr` for a sync from `ghcr.io`. Pull
succeeds via mirror → Docker `hosts.toml` works, proceed. Pull fails (dockerd tries
direct ghcr, blocked) → Docker path is a dead end; go buildkit.

**Rollout order (learned the hard way in Tier 1):** wire the client + verify a real
pull through the mirror **before** removing ghcr/pkg-containers from the allowlist.
Tier 1 briefly broke Docker Hub because the allowlist change landed (via the
since-retired `haku-egress-proxy` kustomization) while the dind change was still blocked (haku-ci
depends on `forgejo`, which re-reconciles on every commit).
