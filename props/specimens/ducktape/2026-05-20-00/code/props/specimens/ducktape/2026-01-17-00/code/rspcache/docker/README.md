# rspcache Docker Image

This directory contains the Docker build assets for the rspcache proxy + admin
services. Use the provided buildx wrapper to get fast iterative builds with a
remote cache stored in the homelab registry.

## Prerequisites

- Docker with the buildx plugin (`docker buildx version` should work)
- Access to `registry.k3s.agentydragon.com` over HTTPS (trust the homelab CA/Authentik so Docker can push directly)
- Credentials for the registry

## Cached build + push

```bash
./adgn/docker/rspcache/buildx.sh
```

The helper script:

- Tags the image with `${TAG}` (defaults to `latest`)
- Builds via Docker buildx and targets `linux/amd64`
- Pushes to `${REGISTRY}` (defaults to `registry.k3s.agentydragon.com`)
- Stores the BuildKit cache at `${REGISTRY}/rspcache:cache`

Environment variables you can override:

- `TAG`: image tag (defaults to `latest`)
- `REGISTRY`: defaults to `registry.k3s.agentydragon.com`
- `CACHE_REF`: defaults to `${REGISTRY}/rspcache:cache`
- `PLATFORMS`: defaults to `linux/amd64`
- `BUILDER_NAME`: buildx builder name (`rspcache-buildx`)

To inspect the cache manifest:

```bash
docker buildx imagetools inspect ${CACHE_REF}
```
