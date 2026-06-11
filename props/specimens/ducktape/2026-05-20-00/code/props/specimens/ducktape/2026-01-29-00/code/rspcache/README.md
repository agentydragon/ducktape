# rspcache

OpenAI-compatible proxy with PostgreSQL-backed response caching, streaming support, and client API key management.

## Quick Start

```bash
# Start the proxy
rspcache --app proxy --host 127.0.0.1 --port 8000

# Start the admin UI
rspcache --app admin --host 127.0.0.1 --port 8100
```

## Environment Variables

| Variable                   | Description                                                                         |
| -------------------------- | ----------------------------------------------------------------------------------- |
| `ADGN_RESP_DB_URL`         | PostgreSQL DSN (required), e.g. `postgresql+asyncpg://user:pass@localhost/rspcache` |
| `OPENAI_API_KEY`           | Default upstream OpenAI key                                                         |
| `ADGN_OPENAI_KEYS`         | Additional keys as `alias=key,...`                                                  |
| `ADGN_OPENAI_KEY_<ALIAS>`  | Per-alias key environment variables                                                 |
| `RSPCACHE_REQUIRE_API_KEY` | Set `true`/`1` to enforce client tokens                                             |

## Client API Keys

Mint tokens via the admin UI or CLI. Tokens use format `sk-rsp_<random>` and can target specific upstream key aliases.

### Upstream Key Aliases

- `OPENAI_API_KEY` → alias `default`
- `ADGN_OPENAI_KEYS="primary=sk-...,fallback=sk-..."` → aliases `primary`, `fallback`
- `ADGN_OPENAI_KEY_FOO` → alias `foo`

## CLI Commands

```bash
rspcache run --app proxy|admin           # Launch ASGI app
rspcache keys mint --name ember          # Create client token
rspcache keys list                       # List tokens
rspcache keys revoke --name ember        # Revoke token
```

## Admin UI Development

```bash
cd adgn/rspcache_admin_ui
npm install
npm run build      # Build to rspcache/admin_ui/dist/
npm run typegen    # Regenerate API types
```

## Container Build

```bash
cd adgn/rspcache_admin_ui && npm install && npm run build
cd ../../
docker build -t registry.k3s.agentydragon.com/rspcache:<tag> -f adgn/docker/rspcache/Dockerfile .
```

## Kubernetes Deployment

The Helm chart (`k8s/helm/rspcache/`) provisions:

- PostgreSQL StatefulSet (10 Gi PVC)
- Proxy Deployment/Service (`/v1/responses`)
- Admin Deployment/Service with Authentik-protected Ingress

```bash
cd k8s/helm/rspcache
helm dependency build
helm upgrade --install rspcache . --namespace rspcache --create-namespace --wait
```

### Required Secrets

- Postgres credentials + `ADGN_RESP_DB_URL` (via sealed secrets)
- Upstream OpenAI keys (`rspcache-openai` secret, managed outside git)
