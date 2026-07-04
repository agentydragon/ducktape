# oci-cache

[Zot](https://zotregistry.dev/) as an **on-demand pull-through cache** for upstream
OCI registries. Two goals: fewer external image pulls from the cluster, and letting
in-cluster consumers (agent dind, etc.) reach one internal endpoint instead of
allowlisting every registry CDN.

## Architecture

| Concern            | Choice                                                                                                                                                                                                         |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Registry           | Zot (`ghcr.io/project-zot/zot-linux-amd64`, full image — needs the `sync` extension)                                                                                                                           |
| Durable content    | SeaweedFS S3 `registry-cache` bucket (`seaweedfs/registry-cache-bucket/`) — manifests + blobs                                                                                                                  |
| Dedupe index       | `oci-cache-valkey` `RedisReplication` (Zot `cacheDriver: redis`, `remoteCache`)                                                                                                                                |
| Local state        | none durable — only ephemeral upload staging on `emptyDir`, so the pod reschedules freely                                                                                                                      |
| Placement          | unpinned (soft-prefers OVH region to sit near SeaweedFS); Valkey on `seaweedfs-ovh` (HDD, networked)                                                                                                           |
| S3 credentials     | `s3-identity-registry-cache` Secret (seaweedfs ns), Reflector-mirrored into `oci-cache`, injected as `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`                                                               |
| Exposure (phase 1) | ClusterIP, plain HTTP on `oci-cache.oci-cache.svc:80` (→ container 5000) — no public route, no auth. Port 80 (not 5000) so port-restricted consumers like haku-ci can reach it without a NetworkPolicy change. |

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

## Eviction

Time/count-based, **not** a hard byte cap. A cached tag is kept if pulled within the
last 30d (`pulledWithin: 720h`) **or** among the 20 most-recently-pulled per repo;
everything else ages out and `gc` reclaims its blobs from S3. Tune `pulledWithin` /
`mostRecentlyPulledCount` in `app/config.json` to your storage budget. Never add a
SeaweedFS bucket-lifecycle rule under Zot — deleting blobs out from under the registry
corrupts manifests.

## Phase 2 (not yet wired)

The public, authenticated endpoint and node-level pull-through are deliberately deferred
— nothing here depends on them, and Talos machine-config changes reboot nodes.

1. **Authenticated public endpoint** at `oci-cache.allegedly.works`. The gateway already
   terminates `*.allegedly.works`, so this is an `HTTPRoute` to the `oci-cache` Service
   plus Zot htpasswd auth. Generate the credential from the devshell and add it as a SOPS
   Secret mounted at `/etc/zot/htpasswd`, with `http.auth.htpasswd.path` in `config.json`:

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

4. **Haku dind allowlist** — point Haku CI's dind `--registry-mirror` at this Service and
   drop the registry CDN FQDNs from `cluster/k8s/agents/haku-egress-proxy/`
   (`cnp-haku-cloud-api-egress.yaml`, the `TODO(pull-through-cache)`). No egress-policy
   change needed: the Service is on port 80, which haku-ci's `toEntities: cluster` rule
   already permits (the claude/haku sandboxes have unrestricted cluster egress).

## Verify before trusting it

Config points not smoke-tested at authoring time (no live cluster access): the pinned
Zot image tag (`v2.1.11`), the exact S3 `storageDriver` keys against this Zot version
(see [zot#3571](https://github.com/project-zot/zot/issues/3571) re 2.1.10 S3 config
drift), and the `sync` prefix/`stripPrefix` routing. Smoke test:

```bash
kubectl -n oci-cache run probe --rm -it --image=curlimages/curl --restart=Never -- \
  curl -sS http://oci-cache/v2/docker-hub/library/alpine/manifests/latest -I
```
