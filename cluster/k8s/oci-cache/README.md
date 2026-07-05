# oci-cache

[Zot](https://zotregistry.dev/) as an **on-demand pull-through cache** for upstream
OCI registries. Two goals: fewer external image pulls from the cluster, and letting
in-cluster consumers (agent dind, etc.) reach one internal endpoint instead of
allowlisting every registry CDN.

## Architecture

| Concern           | Choice                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Registry          | Zot (`ghcr.io/project-zot/zot-linux-amd64`, full image — needs the `sync` extension)                                                                                                                                                                                                                                                                                                                                                       |
| Durable content   | SeaweedFS S3 `registry-cache` bucket (`seaweedfs/registry-cache-bucket/`) — manifests + blobs                                                                                                                                                                                                                                                                                                                                              |
| Dedupe index      | `oci-cache-valkey` `RedisReplication` (Zot `cacheDriver: redis`, `remoteCache`)                                                                                                                                                                                                                                                                                                                                                            |
| Local state       | none durable — only ephemeral upload staging on `emptyDir`, so the pod reschedules freely                                                                                                                                                                                                                                                                                                                                                  |
| Placement         | Zot is unpinned; Valkey is hard-pinned to `hil-ovh` while it uses `seaweedfs-ovh`, because only the OVH nodes run the SeaweedFS CSI driver. Track the planned Valkey move to `local-path-*` in [#2918](https://github.com/agentydragon/ducktape/issues/2918). Valkey holds rebuildable cache metadata/dedupe state; losing it is acceptable, but expect brief cache misses and possible Zot restart/Valkey flush for stale metadb entries. |
| S3 credentials    | `s3-identity-registry-cache` Secret (seaweedfs ns), Reflector-mirrored into `oci-cache`, injected as `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`                                                                                                                                                                                                                                                                                           |
| Internal exposure | ClusterIP, plain HTTP on `oci-cache.oci-cache.svc:80` (→ Zot container 5000), no auth. This is intentional: dockerd's Docker Hub `--registry-mirror` probe does not attach Docker-config credentials for the mirror host. (Port 80 is for conventional addressing; a port-restricted egress policy must still allow the **backend** port 5000 — Cilium enforces egress on the translated targetPort, not the Service port.)                |
| Public exposure   | `https://oci-cache.allegedly.works` routes to the nginx `public-auth-proxy` sidecar on Service port 8080. The sidecar enforces the `puller-credential` htpasswd and then proxies to the unauthenticated in-pod Zot listener.                                                                                                                                                                                                               |

Upstreams are addressed **by path prefix** (Zot `sync` `stripPrefix`); the endpoint
is plain HTTP on port 80, so clients need an `http://` endpoint / insecure-registry config:

| Upstream          | Pull as                                     |
| ----------------- | ------------------------------------------- |
| `docker.io`       | `oci-cache.oci-cache.svc/docker-hub/<repo>` |
| `ghcr.io`         | `oci-cache.oci-cache.svc/ghcr/<repo>`       |
| `quay.io`         | `oci-cache.oci-cache.svc/quay/<repo>`       |
| `registry.k8s.io` | `oci-cache.oci-cache.svc/k8s/<repo>`        |
| `gcr.io`          | `oci-cache.oci-cache.svc/gcr/<repo>`        |

e.g. `docker.io/library/nginx` → `oci-cache.oci-cache.svc/docker-hub/library/nginx`.

Docker Hub is **also** served at the **root** (`oci-cache.oci-cache.svc/library/nginx`),
so it works with Docker's `--registry-mirror` (which is Hub-only and can't take a path
prefix). Note the root is a catch-all — it's listed last so the prefixed registries match
first.

Because that root endpoint is used by classic dockerd as a Docker Hub mirror, keep Zot's
`http.compat: ["docker2s2"]` setting. Docker Hub's multi-arch indexes can reference child
manifests with media type `application/vnd.docker.distribution.manifest.v2+json`; without
`docker2s2`, Zot rejects those children as `invalid manifest content`, returns a failed
mirror response, and dockerd falls back to `https://registry-1.docker.io/v2/`. In haku-ci
that fallback is intentionally not a reliable path through the egress fence.

## Consumers

- **haku-ci dind** pulls Docker Hub base images via `--registry-mirror` (see
  `cluster/k8s/haku-ci/deployment.yaml`); the Docker Hub FQDNs are consequently dropped
  from the `haku-egress-proxy` allowlist. ghcr/quay aren't mirrored through haku-ci yet,
  but this is intentionally downprioritized while Haku image builds move to Bazel
  `rules_oci`: the remaining haku-ci Dockerfile path uses Docker Hub bases, which are
  already covered.

## Eviction

Time/count-based, **not** a hard byte cap. A cached tag is kept if pulled within the
last 30d (`pulledWithin: 720h`) **or** among the 20 most-recently-pulled per repo;
everything else ages out and `gc` reclaims its blobs from S3. Tune `pulledWithin` /
`mostRecentlyPulledCount` in `app/config.json` to your storage budget. Never add a
SeaweedFS bucket-lifecycle rule under Zot — deleting blobs out from under the registry
corrupts manifests.

## Node-level pull-through (not yet wired)

The public authenticated endpoint is wired, but node-level pull-through is deliberately
deferred because Talos machine-config changes reboot nodes.

1. **Public credential rotation**. Generate the credential from the devshell and update
   `app/puller-credential.sops.yaml`; `htpasswd` is mounted into the nginx public-auth
   sidecar, while `config.json` is reflected into haku-ci for clients that explicitly pull
   from `oci-cache.allegedly.works`:

   ```bash
   htpasswd -nbBC 10 puller "$(openssl rand -base64 30)"   # -> puller:$2y$10$...
   ```

2. **Talos containerd mirrors** — point node pulls at the endpoint in
   `cluster/terraform/main/infrastructure.tf` (`machine.registries.mirrors`), with
   `machine.registries.config.<host>.auth` for the htpasswd cred. **Keep `skipFallback`
   false** so nodes fall back to the origin registry when the cache is down (avoids a
   pull-time SPOF and the CNI-bootstrap cycle).

3. **Authenticated Docker Hub upstream** (optional, higher rate limits) — add a
   `credentialsFile` to Zot's `sync` config keyed by `registry-1.docker.io` with a Docker
   Hub username + PAT. Anonymous pull-through works without it.

4. **Haku dind allowlist — ghcr/quay (deferred).** Docker Hub already routes through the
   mirror (see Consumers above). Dropping `ghcr.io` + `pkg-containers.githubusercontent.com`
   from `cnp-haku-cloud-api-egress.yaml` would require haku-ci's dind to mirror ghcr too,
   which classic dockerd can't — enable Docker's containerd image store + `hosts.toml`, or
   move to buildkit. The mirror side already works (`/v2/ghcr/... → 200`), but this is not
   worth doing unless post-`rules_oci` haku-ci still has frequent or painful ghcr/quay pulls.
   Full deferred handoff notes: <plans/tier2-ghcr-quay-mirror.md>. Egress note: haku-ci's
   force-proxy policy had to allow the **backend** port 5000 for the mirror (Cilium enforces
   on targetPort, not the Service port).

## Verified working (2026-07-05)

Confirmed live end-to-end: image tag `v2.1.11` runs with the `sync` extension, the S3
`storageDriver` keys are correct, the `prefix`/`destination` routing resolves
(`docker-hub/library/busybox` → `docker.io/library/busybox`), Docker Hub anonymous pull
works, and a fresh image **persists to the SeaweedFS bucket** (`successfully synced image`
in Zot's log; tag reads back). Getting here took three fixes on top of the initial deploy:
`sync.downloadDir` (crashloop), the `prefix:"**"` + `destination` routing (was mapping
backwards), and a `seaweedfs-s3` gateway restart so it loaded the `registry-cache` identity
(see the SeaweedFS S3 identity RCA in `../../docs/lessons_learned/`).

Smoke test (use a **fresh** tag — an image pulled during the pre-fix window can be stuck in
Zot's metadb as "already synced"; clear with `kubectl -n oci-cache rollout restart
deploy/zot` + a Valkey flush):

```bash
kubectl -n oci-cache run probe --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sS http://oci-cache/v2/docker-hub/library/busybox/manifests/latest -I
```

Known cosmetic quirk: `GET /v2/_catalog` lists empty even for cached repos (clients pull by
name, not via the catalog, so pulls are unaffected).

Root-mirror validation for haku-ci's dockerd path:

```bash
kubectl -n haku-ci exec deploy/haku-runner -c dind -- \
  docker -H tcp://127.0.0.1:2375 pull --platform=linux/amd64 catthehacker/ubuntu:act-latest
```

This path was last checked with haku-state's push-triggered `validate-state` workflow after
the `docker2s2` fix. A manual `workflow_dispatch` proves the job can pass, but it does not
replace a failed push status on the commit Forgejo shows in the branch badge; use a real
push-triggered run when clearing red default-branch CI.

If the dind log says `no basic auth credentials` for
`http://oci-cache.oci-cache.svc.cluster.local/v2/...`, Zot auth has leaked back onto the
internal listener. Keep auth on the public nginx sidecar only; dockerd cannot authenticate
its automatic Docker Hub registry-mirror probe against an htpasswd-protected mirror.
