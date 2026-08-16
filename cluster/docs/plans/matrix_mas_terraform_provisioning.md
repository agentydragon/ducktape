# Matrix Authentication Service provisioning plan

Status: proposal. This document does not deploy MAS or change the current
Synapse-based Matrix setup.

## Context

The current Matrix bot path provisions users through Synapse's shared-secret
registration API and obtains bot credentials from Synapse. Enabling Matrix
Authentication Service (MAS) moves user and session ownership to MAS, so that
provisioner would no longer be the right control plane.

The existing Authentik integration is still useful: Authentik can remain the
upstream OIDC identity provider while MAS owns Matrix users, sessions, and
access tokens. MAS's migration tooling can import Synapse password hashes,
devices, sessions, access tokens, and upstream-provider mappings, but the
cutover requires a planned homeserver migration window.

## Proposed approach

Use the existing Kubernetes/Flux and Terraform GitOps layers for the static
deployment, and use the generic
[Mastercard/restapi Terraform provider](https://registry.terraform.io/providers/Mastercard/restapi/latest)
for MAS's Admin API. A dedicated MAS Terraform provider should not be needed.

### 1. Deploy and configure MAS declaratively

Add MAS alongside Synapse in the `matrix` namespace with:

- a dedicated PostgreSQL database and persistent configuration;
- SOPS-managed signing keys, database credentials, and OIDC client secrets;
- MAS configuration served through a ConfigMap/Secret and reconciled by Flux;
- the MAS Admin API enabled on an internal listener;
- a statically configured OAuth client allowed by `policy.data.admin_clients`.

The static admin client is the bootstrap boundary: Terraform can use its
client credentials to obtain an access token with the `urn:mas:admin` scope.

### 2. Keep Authentik as the upstream IdP

Adapt the existing Matrix Authentik provider wiring so MAS uses it as an
upstream OIDC provider. During migration, preserve the Synapse provider ID
mapping (`synapse_idp_id`) so existing Authentik-linked users are associated
with the corresponding MAS identities.

The current Synapse `oidc_providers` block would move into MAS configuration;
it should not remain as a second, independently managed identity source.

### 3. Manage service users through the MAS Admin API

Create a new Terraform GitOps module, likely
`tf/gitops/matrix-mas/`, with a `Mastercard/restapi` provider configured for
MAS client credentials:

```hcl
provider "restapi" {
  uri                  = var.mas_url
  write_returns_object = true

  oauth_client_credentials {
    oauth_client_id       = var.mas_admin_client_id
    oauth_client_secret   = var.mas_admin_client_secret
    oauth_token_endpoint  = "${var.mas_url}/oauth2/token"
    oauth_scopes          = ["urn:mas:admin"]
  }
}
```

The provider's nested `id_attribute` support matches MAS's wrapped responses:

- create users with `POST /api/admin/v1/users` and read them with
  `GET /api/admin/v1/users/{id}`;
- use `id_attribute = "data/id"` to retain the MAS user ULID;
- create a bot personal session with
  `POST /api/admin/v1/personal-sessions`;
- use the Matrix client API scope, an explicit human-readable name, and a
  chosen expiry policy;
- map Terraform destroy for a personal session to MAS's
  `POST /api/admin/v1/personal-sessions/{id}/revoke` action.

MAS has no user deletion endpoint, only deactivation. Bot users should
therefore be treated as stable identities and protected with
`prevent_destroy`; deactivation is an explicit administrative operation.

### 4. Publish the bot token to the egress path

MAS returns a personal access token only when the session is created. The
initial implementation can extract that creation response and write the token
to the Kubernetes Secret consumed by iron-proxy. OpenClaw continues to use an
access token, with iron-proxy holding the real value and replacing the
placeholder in the outbound `Authorization` header.

This has a deliberate trade-off: the token would be present in Terraform
state. Before implementing this path, verify that the in-cluster Terraform
state backend and controller logs provide an acceptable boundary. If they do
not, keep Terraform responsible for the MAS user only and use a small
in-cluster reconciler to create/regenerate the personal session and write the
Kubernetes Secret directly.

### 5. Migrate and remove the legacy path

The rollout should be staged:

1. Deploy MAS and run the migration checker and dry run.
2. Configure Authentik as the MAS upstream and verify user mappings.
3. Take the planned homeserver downtime and run the Synapse-to-MAS import.
4. Enable MAS integration and its compatibility endpoints in Synapse.
5. Apply the Terraform MAS resources and verify the bot's Matrix access.
6. Switch OpenClaw and iron-proxy from password placeholders to the MAS token.
7. Remove the shared-secret bot registration, Synapse bot-password Secret, and
   legacy Matrix user provisioner only after all consumers have migrated.

MAS's migration guide describes this operation as non-trivial and not easily
reversible, so this should be a separate migration PR from the current
plain-Synapse Matrix integration.

## References

- [MAS Admin API](https://element-hq.github.io/matrix-authentication-service/topics/admin-api.html)
- [MAS Admin API schema](https://element-hq.github.io/matrix-authentication-service/api/spec.json)
- [MAS authorization and personal sessions](https://element-hq.github.io/matrix-authentication-service/topics/authorization.html)
- [MAS migration guide](https://element-hq.github.io/matrix-authentication-service/setup/migration.html)
- [MAS upstream OIDC setup](https://element-hq.github.io/matrix-authentication-service/setup/sso.html)
- [Current plain-Synapse Matrix integration](https://github.com/agentydragon/ducktape/pull/4103)
