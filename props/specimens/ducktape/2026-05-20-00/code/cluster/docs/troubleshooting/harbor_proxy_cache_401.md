# Harbor Proxy Cache: Upstream 401 Failures

**Date**: 2026-02-24
**Status**: Fixed — missing `overridePath` in Talos registry mirror config

## Root Cause

Talos registry mirror endpoints include a path (`/v2/docker-hub-proxy`), but
containerd's `hosts.toml` lacked `override_path = true`. Without it, containerd
**prepends** the endpoint path to the V2 API path instead of replacing `/v2/`:

```text
Expected:  /v2/docker-hub-proxy/library/busybox/manifests/latest        ← single /v2/
Actual:    /v2/docker-hub-proxy/v2/library/busybox/manifests/latest     ← double /v2/
```

Harbor sees the repo name as `docker-hub-proxy/v2/library/busybox`, strips the project
prefix, and tries to fetch `v2/library/busybox` from Docker Hub — which doesn't exist.
Docker Hub returns 401.

**Proof**: Direct curl with the correct URL from inside harbor-core returns HTTP 200:

```bash
# Single /v2/ — works (Harbor fetches from Docker Hub successfully)
curl -H "Authorization: Bearer $TOKEN" \
  'http://localhost:8080/v2/docker-hub-proxy/library/busybox/manifests/latest'
# → 200 OK, full OCI image index
```

## Fix

Added `overridePath = true` to each mirror entry in
`terraform/main/main.tf`. This makes Talos set
`override_path = true` in the generated `hosts.toml`, so the endpoint path
replaces `/v2/` instead of being prepended.

Apply live via `talosctl patch machineconfig` or `tofu apply` (no cluster rebuild needed).

## Symptoms

1. **Image pulls through Harbor proxy fail silently.** Containerd tries the Harbor mirror
   first, gets a 404 (cache miss + failed upstream proxy), then falls back to Docker Hub
   directly. Pulls succeed but bypass the cache entirely.

2. **Harbor core logs show upstream 401 errors:**

   ```text
   [ERROR] [/server/middleware/repoproxy/proxy.go:162]:
     failed to proxy manifest, fallback to local,
     request uri: /v2/docker-hub-proxy/v2/library/busybox/manifests/latest?ns=docker.io,
     error: http status code: 401, body:
   ```

   Note the **double `/v2/`** in the request URI — this is the smoking gun.

3. **Affects all proxy cache projects**, not just Docker Hub:
   - Docker Hub (`docker-hub-proxy`): `401` with empty body
   - GHCR (`ghcr-proxy`): `403 DENIED` — `"requested access to the resource is denied"`
     (GHCR private repos also need credentials — separate issue)

4. **Slow image pulls on Proxmox nodes.** Without cache, every pull goes to the upstream
   registry over the home internet connection.

## Observed Request Flow (from Harbor nginx access logs)

```text
# 1. Containerd → Harbor: initial V2 manifest request (no auth)
HEAD /v2/docker-hub-proxy/v2/library/busybox/manifests/latest?ns=docker.io → 401

# 2. Containerd → Harbor: token exchange (anonymous, public project)
GET /service/token?scope=repository:docker-hub-proxy/v2/library/busybox:pull → 200

# 3. Containerd → Harbor: retry with token
HEAD /v2/docker-hub-proxy/v2/library/busybox/manifests/latest?ns=docker.io → 404
```

Step 3 returns 404 because Harbor has no local cache and the upstream proxy fetch fails
(Harbor looks up the wrong repo name `v2/library/busybox` on Docker Hub).

## Diagnosis

```bash
# Check the Talos-generated hosts.toml — look for missing override_path
talosctl -n <node-ip> read /etc/cri/conf.d/hosts/docker.io/hosts.toml

# Should show:
# [host.'https://registry.allegedly.works/v2/docker-hub-proxy']
#   capabilities = ['pull', 'resolve']
#   override_path = true              ← THIS LINE MUST BE PRESENT

# Check Harbor core logs for double /v2/ in request URIs
kubectl logs -n harbor deployment/harbor-core --tail=50 | grep repoproxy
```

## Configuration

### Talos Registry Mirrors (`main.tf`)

```hcl
"docker.io" = {
  endpoints = [
    "https://registry.allegedly.works/v2/docker-hub-proxy",
    "https://registry-1.docker.io",
  ]
  overridePath = true  # Required — path replaces /v2/, not prepended
}
```

### Harbor Registry Endpoints (`/api/v2.0/registries`)

All endpoints configured with **no credentials** (`"credential": {}`):

| ID  | Name         | URL                         | Type            | Status  |
| --- | ------------ | --------------------------- | --------------- | ------- |
| 1   | k8s-registry | <https://registry.k8s.io>o> | docker-registry | healthy |
| 2   | gcr          | <https://gcr.io>o>          | docker-registry | healthy |
| 3   | dockerhub    | <https://hub.docker.com>m>  | docker-hub      | healthy |
| 5   | ghcr         | <https://ghcr.io>o>         | github-ghcr     | healthy |
| 4   | quay         | <https://quay.io>o>         | docker-registry | healthy |

## Remaining Issue: GHCR Private Repos

GHCR proxy returns `403 DENIED` for private repos like `openclaw/openclaw`. This is
a separate issue — the GHCR registry endpoint needs credentials (GitHub PAT with
`read:packages` scope). Tracked in plan.md.

## Impact

Low severity. All image pulls succeed via fallback to direct upstream. The only
impact is slower pulls on Proxmox nodes (home internet vs. cached in Harbor on
Proxmox storage). VPS nodes are less affected since they have fast datacenter
connectivity to upstream registries.
