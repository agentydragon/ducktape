# authentik_rbac_permission_role — Broken Upstream Since Inception

## Symptom

Terraform `authentik_rbac_permission_role` resource fails with:

```
Error: 405 Method Not Allowed
POST /api/v3/rbac/permissions/assigned_by_roles/{uuid}/assign/
```

This blocks `sso-providers-tf` reconciliation and ~20 dependent Flux Kustomizations.

## Root Cause

The `assign` (and `unassign`) actions on `RoleAssignedPermissionViewSet` are **dead code** — the Django URL configuration never registers routes for `detail=True` actions.

### URL Registration (broken)

`authentik/rbac/urls.py`:

```python
("rbac/permissions/assigned_by_roles", RoleAssignedPermissionViewSet, "permissions-assigned-by-roles")
```

The pattern **lacks `{pk}` capture group**. DRF requires `{pk}` in the URL to route `detail=True` actions.

### ViewSet (has detail actions but no detail route)

`authentik/rbac/api/rbac_assigned_by_roles.py`:

```python
class RoleAssignedPermissionViewSet(ListModelMixin, GenericViewSet):
    ...

    @action(methods=["POST"], detail=True, ...)
    def assign(self, request, *args, **kwargs):
        ...

    @action(methods=["PATCH"], detail=True, ...)
    def unassign(self, request, *args, **kwargs):
        ...
```

- Only `ListModelMixin` — no `RetrieveModelMixin`, so DRF won't auto-generate a detail base route
- `detail=True` actions (`assign`, `unassign`) require a `{pk}` URL pattern that was never registered
- `self.get_object()` in `assign`/`unassign` would fail even if the URL routed correctly (no `RetrieveModelMixin`)

### What DRF Does

When you register a ViewSet with tuple-style URLs (no DRF router), only the explicitly-named actions get routes. The `detail=True` actions try to build URLs like:

```
rbac/permissions/assigned_by_roles/{pk}/assign/
```

But the registered pattern is:

```
rbac/permissions/assigned_by_roles
```

Django matches the base path, can't find a handler for the `assign` sub-path, returns **405 Method Not Allowed**.

## Upstream Status

**Broken in all versions since inception** — initial commit `e28babb0b8` (July 2024) through current `master` (2026.2.1+).

- `v2024.8` (initial): broken
- `v2025.10`: broken
- `v2026.2.1` (current server): broken
- `master` (2026-05-05): still broken — [urls.py](https://github.com/goauthentik/authentik/blob/master/authentik/rbac/urls.py), [rbac_assigned_by_roles.py](https://github.com/goauthentik/authentik/blob/master/authentik/rbac/api/rbac_assigned_by_roles.py)

## TF Provider

The Terraform provider (`~> 2026.2`, built from `goauthentik/terraform-provider-authentik` `main` branch) calls the endpoint as documented:

- Resource: `pkg/provider/resource_rbac_permission_role.go`
- API client: `goauthentik.io/api/v3 v3.2025120.3` (Dec 2025)
- Provider `v2026.2.0` uses API client `v3.2026020.6` — same endpoint, same bug

The provider is not at fault. It calls the endpoint as specified by the OpenAPI schema at `api.goauthentik.io`, which was generated from the `@extend_schema` annotations in the server code — annotations that document intended behavior, not implemented routing.

## Why This Was Never Caught

1. The `assign`/`unassign` actions are documented in the OpenAPI spec (from `@extend_schema`)
2. The API client (`goauthentik.io/api/v3`) generates methods for them
3. The TF provider uses those methods
4. **But the Django URLs never registered the routes** — so every `assign` call returns 405
5. Nobody tested the `authentik_rbac_permission_role` resource against a live server before merging

## Fix (Upstream)

In `authentik/rbac/urls.py`, change to DRF router registration or add `{pk}` pattern:

```python
# Option 1: Add {pk} to the pattern (simplest fix)
("rbac/permissions/assigned_by_roles/(?P<pk>[^/.]+)", RoleAssignedPermissionViewSet, "permissions-assigned-by-roles")

# Option 2: Use DRF router (proper fix, auto-generates detail routes)
from rest_framework.routers import DefaultRouter
router = DefaultRouter()
router.register("rbac/permissions/assigned_by_roles", RoleAssignedPermissionViewSet)
```

Also need `RetrieveModelMixin` on the ViewSet for `self.get_object()` to work:

```python
class RoleAssignedPermissionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    ...
```

## Workaround

Each Authentik object has exactly one owner, split by the constraints both sides impose:

| Owner               | Objects                                                                                                                                       | Reason                                                                                                                         |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Terraform           | `authentik_user.claude_service_account`, `authentik_token.claude_api`, `kubernetes_secret.claude_authentik_token`                             | TF has no `authentik_token` data source, so the token's `.key` is only obtainable as the `resource` create-time response.      |
| Authentik blueprint | `authentik_rbac.role` (`claude-diagnostics`) + its permissions, `authentik_core.group` (`claude-diagnostics`) binding the role to the TF user | Permission assignment goes through the broken `assign` REST endpoint in TF; the blueprint reconciles via internal Python APIs. |

The blueprint binds the TF-managed user into the group via `!Find [authentik_core.user, [username, claude-service-account]]` — no double ownership. The blueprint is mounted into the Authentik server via the `authentik-sso-blueprints` configMapGenerator in <cluster/k8s/authentik/app/kustomization.yaml>. The token value flows `authentik_token.claude_api.key` → `kubernetes_secret.claude_authentik_token` → Reflector → `claude-sandbox`.

### State migration (one-time)

The previous version of <tf/gitops/sso-providers/service_account_claude.tf> had `authentik_rbac_role.claude_diagnostics` and `authentik_group.claude_diagnostics` as TF-owned resources. To hand them off to the blueprint without an API DELETE (which would cause a permissions outage during the gap before the next blueprint reconciliation), the TF file carries `removed { from = ...; lifecycle { destroy = false } }` blocks for both, tagged `CLEANUP(added 2026-05-05)`. After the migration apply has run against `sso-providers-tf` in production and the blueprint has reconciled them as its own, the `removed` blocks should be deleted in a follow-up.

### Long-term fix

The cleanest endpoint is **upstream**: add an `authentik_token` data source to `goauthentik/terraform-provider-authentik` (mirror `data_source_user.go` against the existing `view_key` endpoint). With that data source available, the token can move into the blueprint too and TF collapses to a single `kubernetes_secret` referencing `data.authentik_token.claude_api.key`.

## References

| Item                                    | Location                                                                                       |
| --------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Server URL config                       | `authentik/rbac/urls.py` in `goauthentik/authentik`                                            |
| ViewSet with broken actions             | `authentik/rbac/api/rbac_assigned_by_roles.py`                                                 |
| TF provider resource                    | `pkg/provider/resource_rbac_permission_role.go` in `goauthentik/terraform-provider-authentik`  |
| Our TF config (user, token, k8s secret) | `tf/gitops/sso-providers/service_account_claude.tf`                                            |
| Our blueprint YAML                      | `cluster/k8s/authentik/app/blueprints/claude-service-account.yaml`                             |
| Blueprint kustomization wiring          | `cluster/k8s/authentik/app/kustomization.yaml` (`authentik-sso-blueprints` configMapGenerator) |
| Initial broken commit                   | `e28babb0b8` in `goauthentik/authentik` (July 2024)                                            |
| Current server version                  | `ghcr.io/goauthentik/server:2026.2.1`                                                          |
| TF provider constraint                  | `~> 2026.2` (from `tf/gitops/sso-providers/main.tf`)                                           |
| API client (in provider)                | `goauthentik.io/api/v3 v3.2025120.3`                                                           |
