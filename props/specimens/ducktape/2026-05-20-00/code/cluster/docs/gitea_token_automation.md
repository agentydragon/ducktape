# Gitea Admin Token Automation

## Problem

Gitea SSO configuration via Terraform requires an admin API token. Need declarative token generation
that survives destroy/bootstrap cycles.

## Selected approach

Deploy a Kubernetes Job that calls Gitea's API to create an admin token using
BasicAuth with ESO-generated admin password.

### Why This Approach

| Option                 | Verdict         | Reason                                         |
| ---------------------- | --------------- | ---------------------------------------------- |
| K8s Job + curl API     | ✅ **Selected** | Declarative, minimal deps, well-documented API |
| Gitea CLI in container | ❌ Rejected     | Complex volume mounts, file permissions        |
| Gitea Operator         | ❌ Rejected     | Unmaintained, adds complexity                  |
| Helm chart hook        | ❌ Rejected     | Requires forking chart                         |
| Manual bootstrap token | ❌ Rejected     | Violates turnkey requirement                   |

### Architecture

```text
ESO generates admin password → gitea-admin-password secret
  ↓
Gitea pod initializes with admin user
  ↓
Job calls POST /api/v1/users/admin/tokens (BasicAuth)
  ↓
Job stores token in gitea-admin-token secret
  ↓
Terraform reads token, configures Gitea OAuth with Authentik
```

### Implementation

Job manifest: `k8s/gitea/admin-token/job.yaml`

Required token scopes: `write:admin`, `write:repository`, `write:user`, `write:organization`

Idempotency: First run creates token (201), subsequent runs skip if exists (API returns 500 for duplicate name).
