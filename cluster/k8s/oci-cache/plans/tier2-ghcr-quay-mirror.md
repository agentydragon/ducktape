# Tier 2: route haku-ci dind ghcr/quay pulls through oci-cache

Handoff notes. **Goal:** drop `ghcr.io` + `pkg-containers.githubusercontent.com`
from the `haku-egress-proxy` allowlist
(`cluster/k8s/agents/haku-egress-proxy/cnp-haku-cloud-api-egress.yaml`) by making
haku-ci's dind pull ghcr (and ideally quay) through the in-cluster `oci-cache` Zot
mirror — the same way Docker Hub already does (Tier 1).

## State: mirror ready, client side blocked

**The mirror already serves ghcr/quay.** Verified 2026-07-04 from a curl pod:

```text
GET http://oci-cache.oci-cache.svc/v2/ghcr/project-zot/zot-minimal-linux-amd64/manifests/v2.1.11
-> HTTP 200
```

So `oci-cache` needs no changes for Tier 2 — the `/ghcr` and `/quay` prefixes work.
The whole problem is the **client** (haku-ci's dind): getting it to send ghcr pulls
to the mirror.

## The blocker: classic dockerd only mirrors Docker Hub

haku-ci runs `docker:27-dind-rootless` (`cluster/k8s/haku-ci/deployment.yaml`).
Tier 1 works because Docker's `--registry-mirror` handles Docker Hub — but it is
**Hub-only** and takes **no path**, which is also why `oci-cache` serves Docker Hub
at its root (a 6th Zot sync entry, destination `/`). There is **no `--registry-mirror`
equivalent for ghcr/quay** in classic dockerd.

Getting per-registry (ghcr/quay) mirroring needs one of:

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

## Why this wasn't finished: couldn't test the client path

- **No access to haku-ci.** It's operator-only with **no Haku RBAC** (by design —
  see `cluster/k8s/haku-ci/README.md`), so an agent authenticating as `haku-k8s`
  cannot create/exec pods there.
- **haku-sandbox enforces `baseline` PodSecurity.** It's the only namespace the Haku
  agent can write to, but it **rejects `privileged` pods**:
  `violates PodSecurity "baseline:latest": privileged`. Rootless dind still needs
  `privileged: true` (per haku-ci's own comment — the non-privileged path can't
  create the rootless TAP), so a dind can't run there to test the Docker `hosts.toml`
  behaviour.

To finish, the next agent needs **a privileged-capable namespace** (relax
haku-sandbox to `privileged`/`baseline`-warn, or use another ns), or run the test
**in haku-ci itself** with operator/admin credentials.

## The empirical test to run

Stand up a `docker:27-dind-rootless` pod (privileged) with the containerd image
store + `hosts.toml`, and **no `HTTP_PROXY`** so the only route to ghcr is the mirror
— a successful ghcr pull then proves `hosts.toml` routing works.

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
`haku-egress-proxy` kustomization) while the dind change was still blocked (haku-ci
depends on `forgejo`, which re-reconciles on every commit).

## oci-cache reference (for whoever picks this up)

- Service is plain HTTP on `:80` (Service) → `:5000` (pod). Clients need
  `insecure-registry` / an `http://` endpoint.
- **Cilium gotcha:** a port-restricted egress policy must allow the **backend port
  5000**, not the Service `:80` — Cilium's socket-LB enforces on the translated
  targetPort. haku-ci's `ccnp-force-proxy-egress.yaml` already allows 5000. Full
  writeup: `cluster/docs/cilium_network_policy.md`.
- Zot `sync` mapping: `content.prefix` matches the **upstream** repo (use `"**"`),
  `destination` is the local namespace (`/ghcr`, `/quay`, …). Docker Hub is served at
  both `/docker-hub` and root (catch-all, listed last).

## Recommendation

**Hold unless ghcr pulls are actually frequent/painful.** The reward is two stable
GitHub hosts; the Docker path is unverifiable-and-probably-unsupported, and the
reliable path is a buildkit migration. The Docker Hub win (rate limits + the
CloudFlare/CloudFront CDN sprawl) is already captured by Tier 1. The mirror stands
ready whenever the client side is solved.
