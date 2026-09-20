# In-cluster Terraform conversion audit

Date: 2026-09-20

## Summary

The live cluster had 19 Terraform custom resources when checked; this audit excludes
`infra-drift` and covers the other 18 roots. All 18 reported `Ready=True`. Their
deployed configurations contain 283 Terraform `resource` blocks (not counting data
sources, imports, locals, or `for_each` expansions).

There are three useful estimates, with different prerequisites:

| Path                          | Blocks | Share of 283 | What it means                                                                                                                                                                                                                                                                    |
| ----------------------------- | -----: | -----------: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ESO-only candidates           |     11 |         3.9% | Move app password generation and its Kubernetes Secret writes to the already-installed External Secrets Operator. Coordinate the credential handoff with the app/provider.                                                                                                       |
| Authentik + credential bridge |    169 |        59.7% | The 11 above, plus 127 non-token Authentik objects, 24 OAuth client-credential Secrets, and 7 app/session password resources. Requires an Authentik-to-Kubernetes secret bridge; API retrieval is verified with an admin token, while least-privilege access remains unverified. |
| Add ExternalDNS               |    176 |        62.2% | The 169 above plus 7 Route 53 record resources. Requires installing ExternalDNS and safely adopting the existing records.                                                                                                                                                        |

The 283-block denominator includes seven blocks in the private `tf/thrive-scrape`
root, which could not be inspected from this checkout. I conservatively count none of
those seven as convertible. Excluding that unassessed root, the Authentik + credential
bridge estimate is 169/276 = 61.2%.

These are ratios of Terraform resource blocks, used as a consistent proxy for
configuration bulk. They are not line-count, implementation-effort, or operational-risk
estimates. Every conversion replaces HCL with another declarative resource or controller
configuration; it does not remove the desired application state.

## Per-root estimate

The main candidate column is the 169-block Authentik + credential-bridge target.
ExternalDNS and the two webhook-token handoffs are shown separately because they
require additional controllers or coordinated credential rotation.

| Terraform root               | Deployed resource blocks | Main candidates | Main ratio | Additional conditional candidates                                         |
| ---------------------------- | -----------------------: | --------------: | ---------: | ------------------------------------------------------------------------- |
| `agent-machine-access`       |                       80 |              71 |      88.8% | —                                                                         |
| `alloy-otlp-bearer-token`    |                       11 |              11 |       100% | —                                                                         |
| `budget-ledger`              |                        6 |               2 |      33.3% | —                                                                         |
| `cpap-data`                  |                       10 |               4 |      40.0% | —                                                                         |
| `dns-records`                |                        8 |               0 |         0% | +7 with ExternalDNS (87.5% total); domain registration stays in Terraform |
| `flux-webhook-token`         |                        3 |               0 |         0% | +2 after a coordinated webhook-token handoff                              |
| `forgejo-agentydragon`       |                        1 |               0 |         0% | —                                                                         |
| `forgejo-agentydragon-repos` |                       14 |               0 |         0% | —                                                                         |
| `forgejo-claude`             |                        4 |               3 |      75.0% | —                                                                         |
| `forgejo-images`             |                        1 |               0 |         0% | —                                                                         |
| `gatus-sso`                  |                        4 |               4 |       100% | —                                                                         |
| `github-branch-protection`   |                        1 |               0 |         0% | —                                                                         |
| `github-secrets-sync`        |                        9 |               0 |         0% | —                                                                         |
| `haku-cloud-agent`           |                        8 |               0 |         0% | —                                                                         |
| `haku-state`                 |                       19 |               2 |      10.5% | +2 after a coordinated Forgejo webhook-token handoff                      |
| `litellm-keys`               |                       20 |               0 |         0% | —                                                                         |
| `sso-providers`              |                       77 |              72 |      93.5% | —                                                                         |
| `thrive-scrape`              |                        7 |               0 |         0% | Unassessed private source; excluded from candidate numerator              |
| **Total**                    |                  **283** |         **169** |  **59.7%** | **+7 ExternalDNS; +4 webhook handoffs**                                   |

If both webhook tokens are rotated and handed to ESO, the purely arithmetic ceiling
becomes 180/283 = 63.6%. I would not plan against that ceiling: both tokens are coupled
to webhook configuration, and the Haku webhook path is derived from its token. Keep
those in Terraform until the webhook can be updated and verified as one controlled
change.

## Main conversion paths

### 1. Authentik objects: native Blueprints, with a credential bridge

Four roots contain 132 Authentik resource blocks: 62 in `agent-machine-access`, 10 in
`alloy-otlp-bearer-token`, 3 in `gatus-sso`, and 57 in `sso-providers`. Five are
`authentik_token` resources whose keys are returned only when created; for example,
[`service_account_claude.tf`](../tf/gitops/sso-providers/service_account_claude.tf)
stores the returned key in a Kubernetes Secret. Keep those five resources and their
five output Secrets in Terraform unless a controller takes over creation and durable
secret capture. That leaves 127 Authentik object blocks as Blueprint candidates.

This repository already runs Authentik Blueprints, and its SSO notes identify active
Terraform-managed providers as a consolidation target. Blueprints can own the
applications, OAuth/proxy providers, groups, bindings, users, brands, and mappings.
This is the largest opportunity, but it does not by itself deliver the provider
credentials to client pods.

The subtlety raised in the discussion is real: the Blueprint export documentation says
write-only fields such as the OAuth provider secret are omitted. That is different from
the API path used by the Terraform provider: the provider's OAuth resource performs a
retrieve after create and reads `ClientSecret` into its computed sensitive attribute;
the Authentik API reference also lists `client_secret` on provider retrieve. I verified
this against the live Authentik 2026.8.2 API using its existing bootstrap admin token:
the `client_id`-filtered list call returned one matching provider with a nonempty
`client_secret`; the detail call returned the same value as the current Gatus Kubernetes
Secret. The secret value was not printed. This confirms that Authentik exposes the
minted value through the API, but it does not establish that an ESO service account can
read it with appropriately narrow permissions.

The proposed ESO `SecretStore` and `ExternalSecret` also passed a Kubernetes server-side
dry run against the installed ESO 2.10 CRDs. That validates the resource shapes, not an
ESO webhook request or Secret write. ESO's webhook provider supports templated URLs,
headers, and JSONPath response extraction, so the remaining bridge proof is an actual
reconcile using a dedicated Authentik service account.

Do not use the bootstrap admin token for ESO. Authentik recommends a dedicated service
account for API automation and documents object-level permissions. However, its still
open issue [#18233](https://github.com/goauthentik/authentik/issues/18233), reported on
2025.10.1, says object-level view permissions do not make OAuth2/OIDC providers
accessible without global provider-view permission. That issue predates the 2025.12
role-based access-control change and does not prove the same behavior on 2026.8.2, but
it makes provider-scoped visibility an explicit test requirement. Authentik also
backported a security patch named `secrets-read-permission` to its 2026.2 release line;
check the installed-version permission behavior before assuming ordinary provider view
grants reveal `client_secret`.

Two plausible bridges:

1. **ESO Webhook provider:** ExternalSecret reads the provider object through
   Authentik's API and renders its client ID/secret into the existing canonical Secret;
   Reflector or namespace-scoped ExternalSecrets distribute it to consumers. The
   admin-token API calls and ESO schema dry run are confirmed; pilot one provider with a
   dedicated service account and verify authorization, response shape, refresh behavior,
   and rotation before expanding. This is a pull-and-refresh design: it moves Authentik
   secret reads out of Terraform, but ESO still polls Authentik on its refresh schedule.
2. **A Kubernetes Authentik controller:** for a write-through handoff, a controller
   could own selected OAuth providers, POST their configuration without a secret so
   Authentik mints it, then write the resulting credential to a Kubernetes Secret
   immediately. This is controller-triggered delivery into Kubernetes, not an
   Authentik-initiated push.
   This replaces Blueprint ownership for those selected providers; Blueprints can keep
   owning unrelated Authentik objects. The controller would still reconcile desired
   state, but it need not wait for an ESO refresh interval to publish the new value.

The create-response contract needs a direct test before designing around it. The
Terraform provider's implementation takes the provider ID from Authentik's POST response,
then performs a detail GET and reads `client_secret`; it does not rely on a secret in the
POST response. If Authentik returns the minted secret on create, a controller can write
it through without a follow-up read. If it does not, the controller still needs permission
to read the just-created provider. Either way, the controller needs Authentik create
permissions and introduces a new reconciler to own and maintain.

Authentik also supports generic notification webhooks, but the documented payload is an
event notification with configurable event-context mappings, not a provider lifecycle
callback that hands out generated credentials. Do not route provider secrets through
ordinary event notifications. This is not a ready-made push alternative.

The 24 OAuth-linked Secret blocks are included only if one of those bridges works. The
seven additional app/session password blocks are candidates for ESO Password plus
ExternalSecret: three in `agent-machine-access` and four in `sso-providers`. The
acceptance operator's Authentik password, the protected Agentplane encryption/signing
material, the five Authentik token outputs, and their secrets are not included in the
169-block estimate.

### 2. App-generated passwords and Kubernetes Secrets: ESO

Outside the Authentik roots, 11 blocks are good ESO candidates:

- `budget-ledger`: one random password and its Secret (2/6 blocks).
- `cpap-data`: two random passwords and two Secrets (4/10).
- `forgejo-claude`: one random password and two credential Secrets (3/4).
- `haku-state`: the Haku Console agent API password and Secret (2/19).

ESO and its Password generator already run in the cluster. The Password generator is
stateless: each invocation can produce a new value, and it does not remember the prior
one. Do not transfer ownership by letting a new generator overwrite a live credential.
Keep the existing Secret and credential working during handoff, or perform a planned
application credential rotation that updates the remote app and consumer Secret
together. For generated credentials that should remain fixed, use `CreatedOnce` and
consider an immutable target; `CreatedOnce` alone can generate a different value if the
ExternalSecret object is deleted and recreated. Follow the existing
[`forgejo-images` handoff](../tf/gitops/forgejo-images/main.tf), which uses
`removed { lifecycle { destroy = false } }` so Terraform forgets a Secret without
deleting the live object ESO will own.

This moves credential generation and Secret data ownership. It does not move the
Forgejo users, repositories, collaborators, or other Forgejo API resources: this audit
found no installed Forgejo operator that reconciles those objects.

### 3. Route 53 records: ExternalDNS, only if worth installing

[`dns-records`](../tf/gitops/dns-records/main.tf) has seven `aws_route53_record` blocks
plus one registered-domain block.
The record set is representable as ExternalDNS `DNSEndpoint` objects (A and TXT records,
plus MX), while domain registration remains in Terraform. ExternalDNS supports a CRD
source and Route 53, but no ExternalDNS Deployment/image or CRD is installed in this
cluster today. The seven-block conversion therefore requires adding a controller,
scoping its AWS permissions to this hosted zone, choosing TXT ownership, and proving a
no-surprise adoption plan for the records already managed by Terraform. This is a
possible 7/8 reduction in that root, not a current-controller opportunity.

## Keep in Terraform for now

- **Authentik one-time tokens:** five `authentik_token` resources and five Secrets hold
  returned token keys. These need a controller that can capture and persist the key at
  creation; ordinary Blueprints or a later data lookup are not an equivalent handoff.
- **Forgejo and GitHub API objects:** users, repositories, collaborators, SSH keys,
  branch protection, webhooks, and Actions secrets have no matching installed operator
  in this cluster. Terraform remains the available API reconciler.
- **LiteLLM keys/teams:** the app API is the key issuer; no key-management operator is
  installed. Treat key capture/rotation and consumer Secret delivery as one domain if
  designing a controller.
- **Haku Cloud Agent resources:** vendor API resources with no corresponding operator
  found in the cluster.
- **`forgejo-agentydragon-repos` generated passwords:** the two password values exist
  to satisfy Forgejo user creation and are not delivered to consumers as Kubernetes
  Secrets; replacing them with ESO would add coupling without reducing a Secret handoff.
- **`thrive-scrape`:** the seven-block count is from the private Gaffer source revision
  recorded in the live object. Source was unavailable here, so this remains unassessed.

## Suggested sequence

1. Decide whether ESO's refresh-based delivery is acceptable. If so, pilot the smallest
   OAuth provider with a dedicated service account and one ESO Webhook ExternalSecret.
   If write-through delivery is preferred, first inspect Authentik's create response on a
   non-production instance and confirm whether it includes the generated secret; then
   prototype a controller that owns one provider. Keep Terraform as owner until the new
   Secret and app both read the same value through the chosen path.
2. Convert one ESO-only credential set with a documented ownership handoff and a
   preservation/rotation plan. Use that to settle conventions for Secret names,
   `CreatedOnce`, immutability, Reflector, and Flux pruning.
3. Once the bridge is proven, move Authentik objects by app/root to Blueprints and
   secrets to ESO/controller ownership. Use Terraform `removed` blocks with
   `destroy = false` for surviving Kubernetes Secrets; remove the Terraform objects only
   after the new owner has reconciled and consumers are verified.
4. Evaluate ExternalDNS separately. Its 2.5% overall reduction may not justify adding
   another controller unless DNS record churn or Terraform reconciliation is an actual
   operational burden.

## Evidence and references

- Live snapshot: 19 Terraform CRs including `infra-drift`; 18 in scope and all 18
  `Ready=True`. Authentik server/worker image `2026.8.2`; ESO image `v2.10.0`; ESO
  Password generator CRD present; ExternalDNS Deployment and CRDs absent.
- Deployed-revision HCL `resource` block counts, grouped by root and resource type; the
  private `thrive-scrape` count is 7 from its live source revision. `infra-drift` is
  excluded by request.
- Repository SSO ownership/secret flow notes: [`cluster/docs/sso.md`](../cluster/docs/sso.md).
- Authentik says write-only OAuth provider secrets are omitted from Blueprint exports:
  [Blueprint export docs](https://docs.goauthentik.io/customize/blueprints/export).
- Authentik API reference lists `client_secret` on OAuth provider retrieve:
  [provider retrieve API](https://docs.goauthentik.io/docs/developer-docs/api/reference/providers-oauth-2-retrieve).
- Live API verification on Authentik 2026.8.2: list filtered by `client_id` returned one
  match and a nonempty secret; detail retrieve matched the live Gatus Secret. Both calls
  used the bootstrap admin token. No value was printed, and no Authentik or Kubernetes
  object was changed.
- ESO `SecretStore` and `ExternalSecret` shapes passed server-side dry run against ESO
  2.10 CRDs; no ESO reconcile or Secret write was performed. Webhook templating support is
  documented in the [ESO 2.10 webhook provider docs](https://external-secrets.io/v2.10.0/provider/webhook/).
- Authentik's OAuth2 provider model generates a secret by default when one is omitted
  ([model source](https://github.com/goauthentik/authentik/blob/main/authentik/providers/oauth2/models.py)).
  The Terraform provider uses the POST response for the provider ID, then performs a
  detail GET to populate `client_secret` ([implementation](https://github.com/goauthentik/terraform-provider-authentik/blob/main/pkg/provider/resource_provider_oauth2.go#L1491-L1542));
  whether the POST response itself includes the secret remains unverified.
- Authentik's documented [notification webhook](https://docs.goauthentik.io/sys-mgmt/events/transports)
  sends event notifications and supports mappings from event context; it is not
  documented as a provider lifecycle secret-delivery hook.
- Authentik recommends dedicated service accounts for API automation and supports
  object permissions ([service accounts](https://docs.goauthentik.io/users-sources/user/account-types/service-accounts),
  [permissions](https://docs.goauthentik.io/users-sources/access-control/permissions)).
  The still-open [OAuth2 provider object-view issue #18233](https://github.com/goauthentik/authentik/issues/18233)
  is a reason to verify scoped access on the deployed version. A `secrets-read-permission`
  patch was backported to Authentik's 2026.2 branch ([PR #25955](https://github.com/goauthentik/authentik/pull/25955)).
- The upstream [Terraform provider OAuth2 implementation](https://github.com/goauthentik/terraform-provider-authentik/blob/main/pkg/provider/resource_provider_oauth2.go#L1510-L1541)
  reads the provider and sets its `client_secret` attribute.
- ESO documents that Password generator invocations create new values and do not retain
  prior outputs: [Generators](https://external-secrets.io/latest/guides/generator/).
  Secret refresh policies and `CreatedOnce` recreation behavior are documented in
  [ExternalSecret](https://external-secrets.io/main/api/externalsecret/).
- ExternalDNS [CRD source](https://kubernetes-sigs.github.io/external-dns/latest/docs/sources/crd/)
  supports declarative `DNSEndpoint` records, including record types such as A, MX, and
  TXT.
