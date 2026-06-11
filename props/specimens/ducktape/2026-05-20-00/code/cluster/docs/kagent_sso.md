# Kagent SSO Integration Investigation

**Date**: 2025-11-22
**Goal**: Enable Authentik forward auth for Kagent (<https://kagent.test-cluster.agentydragon.com>)

## Architecture Overview

### What is an Outpost?

An **Authentik Outpost** is a component that sits between your application and
users, handling authentication. Think of it as an authentication proxy/gateway.
Outposts can operate in different modes:

- **Proxy mode**: Acts as a reverse proxy (like Authelia)
- **Forward auth mode**: Works with existing reverse proxies (NGINX, Traefik, Envoy)
- **LDAP mode**: Provides LDAP interface for legacy apps
- **RADIUS mode**: Provides RADIUS for network authentication

### The Embedded Outpost

Authentik includes a **built-in embedded outpost** that runs inside the main
Authentik server pod. This is managed by Authentik itself (hence
`managed: goauthentik.io/outposts/embedded`).

**Where it runs:**

- **Pod**: `authentik-server` (same container as the main Authentik application)
- **Service**: `authentik-server.authentik.svc.cluster.local`
- **Port**: 80 (HTTP), 443 (HTTPS)
- **Container Ports**: 9000 (HTTP), 9443 (HTTPS), 9300 (metrics)

**URLs served by embedded outpost:**

- `/outpost.goauthentik.io/auth/nginx` - NGINX forward auth check endpoint
- `/outpost.goauthentik.io/auth/traefik` - Traefik forward auth check endpoint
- `/outpost.goauthentik.io/start` - Authentication initiation/login redirect
- `/outpost.goauthentik.io/callback` - OAuth callback handler
- `/outpost.goauthentik.io/sign_out` - Logout endpoint

The embedded outpost is **part of the same Python/Django process** as the main
Authentik server - it's not a separate binary or container. The URLs are handled
by the same ASGI/WSGI application.

Benefits:

- No separate deployment needed
- Zero-configuration for basic use cases
- Shared resources with Authentik server
- Ideal for simple setups

You can also deploy **external outposts** as separate pods for:

- Performance isolation
- Different network zones
- Geographic distribution

### How Forward Auth Works

Forward auth is a pattern where NGINX (or another reverse proxy) delegates
authentication to an external service:

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Request Flow                                 │
└─────────────────────────────────────────────────────────────────┘

1. User → https://kagent.test-cluster.agentydragon.com
                    │
                    ▼
2. NGINX Ingress (checks auth-url annotation)
                    │
                    ├─→ Auth subrequest to Authentik embedded outpost
                    │   http://authentik-server:80/outpost.goauthentik.io/auth/nginx
                    │
                    ▼
3. Authentik outpost checks:
   - Does user have valid session cookie?
   - Is user authorized for this app (Kagent)?
                    │
        ┌───────────┴───────────┐
        │                       │
        ▼ NO                    ▼ YES
4a. Return 302 redirect    4b. Return 200 OK
    to Authentik login         + user headers
    with ?rd= parameter
        │                       │
        ▼                       ▼
5a. User logs in           5b. NGINX forwards
    via Authentik UI           request to Kagent
        │                       with user info
        ▼                       in headers
6a. Redirect back to
    original URL (?rd=)
        │
        └───────────────────────┘
                    │
                    ▼
7. Request proxied to backend (Kagent pod)
```

### The Key Configuration Pattern

**Important**: The Kagent ingress uses a clever pattern - it routes
`/outpost.goauthentik.io/*` paths on the Kagent domain to the Authentik service.
This means:

- `https://kagent.test-cluster.agentydragon.com/` → Kagent UI
- `https://kagent.test-cluster.agentydragon.com/outpost.goauthentik.io/*` → Authentik embedded outpost

This keeps users on the same domain during auth flow (no visible redirect to auth.test-cluster.agentydragon.com).

**In the Ingress** (kagent-ui):

```yaml
annotations:
  # Where NGINX should check auth (subrequest)
  nginx.ingress.kubernetes.io/auth-url: "http://authentik-server.authentik.svc.cluster.local:80/outpost.goauthentik.io/auth/nginx"

  # Where to redirect if auth fails (login page)
  nginx.ingress.kubernetes.io/auth-signin: "https://kagent.test-cluster.agentydragon.com/outpost.goauthentik.io/start?rd=$scheme://$http_host$escaped_request_uri"

  # User identity headers to forward to backend
  nginx.ingress.kubernetes.io/auth-response-headers: "Set-Cookie,X-authentik-username,X-authentik-groups,X-authentik-email,X-authentik-name,X-authentik-uid"
```

**In Authentik** (embedded outpost):

- The outpost needs to know which **providers** it should handle
- Each provider is associated with an **application** (Kagent)
- When a request comes to `/outpost.goauthentik.io/auth/nginx`, the outpost:
  1. Extracts the original host from headers
  2. Matches it to a provider's `external_host` (kagent.test-cluster.agentydragon.com)
  3. Checks if user is authenticated and authorized for that app
  4. Returns appropriate response to NGINX

### Why Assignment is Required

**The Problem**: The embedded outpost starts with NO providers assigned. It doesn't know about any applications.

When NGINX sends an auth subrequest for `kagent.test-cluster.agentydragon.com`:

- Outpost has no provider matching that host
- Returns error (500)
- NGINX sees auth failure → 500 to user

**The Solution**: Assign the Kagent proxy provider to the embedded outpost:

```json
{
  "providers": [3] // Kagent provider ID
}
```

Now when requests come in:

- Outpost sees provider for `kagent.test-cluster.agentydragon.com`
- Checks authentication/authorization
- Returns proper response (redirect to login or allow)

## Context

Kagent is deployed and accessible at `kagent.test-cluster.agentydragon.com` with
an ingress configured for forward auth via Authentik embedded outpost. The goal
is to protect access with SSO authentication.

### Current State

- **Kagent**: Deployed, pods running (kagent-ui: 1/1 Running)
- **Authentik**: Deployed, recently recreated PostgreSQL database (70+ minutes ago)
- **Embedded Outpost**: Exists but doesn't have Kagent provider assigned
- **Forward Auth**: Configured in ingress but returns 500 error

## Problem Summary

The Kagent proxy provider needs to be assigned to Authentik's embedded outpost for
forward auth to work. Multiple terraform approaches failed due to:

1. Import block syntax not supported
2. `restful_operation` resource missing required arguments
3. `null_resource` + curl provisioner - **curl not available in tf-runner container**

## Key Discoveries

### PostgreSQL Rebuild Impact

Earlier in the session, we recreated the Authentik PostgreSQL pod/PVC to fix
credential issues. This wiped the database, requiring Authentik to bootstrap from
scratch:

- Default flows and groups recreated successfully
- Terraform-managed resources (Grafana, Harbor, Vault OAuth providers) recreated
- **Kagent provider created but NOT assigned to outpost**

### Authentik API Status

- ✅ Flows exist: `default-provider-invalidation-flow`, `default-provider-authorization-implicit-consent`
- ✅ Groups exist: `authentik Admins`
- ✅ Kagent provider created (ID: 3)
- ✅ Embedded outpost exists (UUID: aa904b56-e70b-4f6e-a706-5765ac9fbf2b)
- ❌ Outpost providers: `null` (empty, no assignments)

### Terraform Status

- **authentik-blueprint-kagent**: Failed (Apply error - curl not found)
- **Last attempted**: Commit db57f8e - null_resource with curl provisioner
- **Current state**: Terraform manages provider/application creation but cannot assign to outpost

## Investigation Timeline

1. **Initial approach**: Custom blueprint ConfigMap mounted to Authentik
   - **Issue**: Blueprint discovery ran before provider creation (timing)

2. **Terraform import block**: Tried to import and manage embedded outpost
   - **Issue**: Import block syntax caused "configuration is invalid"

3. **Terraform restful_operation**: Used terraform-provider-restful
   - **Issue**: Provider security config incorrect, then unsupported arguments

4. **Terraform null_resource + wget**: Simple provisioner approach
   - **Issue**: BusyBox wget doesn't support `--method=PATCH`

5. **Terraform null_resource + curl**: Changed to curl
   - **Issue**: tf-runner container doesn't have curl installed

## Current Files

### `/home/agentydragon/code/tf/gitops/sso/kagent/main.tf`

Creates:

- Proxy provider (forward_single mode)
- Application (Kagent)
- Policy binding (authentik Admins group)
- ~~Outpost assignment~~ (removed, manual step required)

### `/home/agentydragon/code/cluster/k8s/kagent/ingress.yaml`

Ingress annotations for forward auth:

- `nginx.ingress.kubernetes.io/auth-url`: Points to embedded outpost
- `nginx.ingress.kubernetes.io/auth-signin`: Redirect to Authentik login

## Resolution

### Provider Assignment (COMPLETED ✅)

**Date**: 2025-11-22 02:30 UTC

Successfully assigned Kagent provider (ID: 3) to embedded outpost via API:

```bash
TOKEN=$(cat /tmp/new_bootstrap_token.txt | tr -d '\n')
curl -X PATCH \
  "https://auth.test-cluster.agentydragon.com/api/v3/outposts/instances/aa904b56-e70b-4f6e-a706-5765ac9fbf2b/" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"providers":[3]}'
```

**Result**: Outpost now has `"providers":[3]` in configuration.

### Forward Auth Verification (WORKING ✅)

**Test**: `curl -I https://kagent.test-cluster.agentydragon.com`

**Result**:

```http
HTTP/2 302
location: https://kagent.test-cluster.agentydragon.com/outpost.goauthentik.io/start?rd=...
set-cookie: authentik_proxy_session_9ee21266-2aae-4c4f-aa15-7df92f6799de; Path=/; ...
```

**Status**: Forward auth working correctly! NGINX successfully:

1. Sends auth subrequest to embedded outpost
2. Outpost recognizes kagent.test-cluster.agentydragon.com host
3. Returns 302 redirect to Authentik login (no more 500 errors)

### Forward Auth Configuration Patterns

Both approaches work for the embedded outpost:

**Approach A: Cross-Domain** (common pattern)

- Auth check: Internal cluster DNS
- Login redirect: `auth.test-cluster.agentydragon.com/outpost.goauthentik.io/start`
- User sees: Domain changes from kagent → auth → kagent

**Approach B: Same-Domain** (Kagent's current setup)

- Auth check: Internal cluster DNS
- Login redirect: `kagent.test-cluster.agentydragon.com/outpost.goauthentik.io/start`
- User sees: Stays on kagent domain throughout (seamless UX)
- Mechanism: Ingress routes `/outpost.goauthentik.io/*` to Authentik service

## Next Steps

1. ✅ ~~Manual provider assignment~~ (COMPLETE)
2. ✅ ~~Verify forward auth redirect~~ (COMPLETE - 302 working)
3. **Test full OAuth flow** (IN PROGRESS):
   - Access Kagent URL in browser
   - Complete Authentik login
   - Verify redirect back to Kagent
   - Check user headers passed to backend
4. **Commit terraform changes** (provider assignment automation removed)
5. **Document solution** in plan.md or TROUBLESHOOTING.md

## Reference Information

### API Endpoints

- Embedded outpost: `/api/v3/outposts/instances/?managed=goauthentik.io%2Foutposts%2Fembedded`
- Outpost detail: `/api/v3/outposts/instances/<uuid>/`
- Providers list: `/api/v3/providers/proxy/`

### IDs

- Embedded outpost UUID: `aa904b56-e70b-4f6e-a706-5765ac9fbf2b`
- Kagent provider ID: `3`
- Bootstrap token location: `/tmp/new_bootstrap_token.txt`

### Ingress Configuration

```yaml
annotations:
  nginx.ingress.kubernetes.io/auth-url: "http://authentik-server.authentik.svc.cluster.local:80/outpost.goauthentik.io/auth/nginx"
  nginx.ingress.kubernetes.io/auth-signin: "https://kagent.test-cluster.agentydragon.com/outpost.goauthentik.io/start?rd=$scheme://$http_host$escaped_request_uri"
```
