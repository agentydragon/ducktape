# In-cluster Terraform conversion audit

Date: 2026-09-21 (live Flux artifact updated 2026-09-21 06:43 UTC)

## Summary

The live cluster had 19 Terraform custom resources when checked; this audit excludes
`infra-drift` and covers the other 18 roots. All 18 reported `Ready=True`. The 17
shared-repository roots and the private `thrive-scrape` root account for 276 deployed
Terraform `resource` blocks (not counting data sources, imports, locals, or `for_each`
expansions). The parked `haku-cloud-agent` Terraform is suspended; its last-applied
configuration still has eight blocks, although current HCL has six.

There are three useful estimates, with different prerequisites:

| Path                                     | Blocks | Share of 276 | What it means                                                                                                                                                                                                                           |
| ---------------------------------------- | -----: | -----------: | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ESO-only candidates                      |     11 |         4.0% | Remaining app-password and Kubernetes Secret handoffs to the already-installed External Secrets Operator. The Airlock session-key handoff is complete and is excluded from this remaining count.                                        |
| Authentik Blueprints + credential bridge |    163 |        59.1% | The 11 above, plus 122 non-token Authentik objects, 23 OAuth client-credential Secrets, and 7 app/session password resources. Secret delivery is unproven and is parked; this is a conditional ceiling, not the current recommendation. |
| Add ExternalDNS                          |    170 |        61.6% | The 163 above plus 7 Route 53 record resources. Requires installing ExternalDNS and safely adopting the existing records.                                                                                                               |

The 276-block deployed denominator includes seven blocks in the private
`tf/thrive-scrape` root, which could not be inspected from this checkout. I conservatively
count none of those seven as convertible. Excluding that unassessed root, the
Authentik-plus-bridge estimate is 163/269 = 60.6%. The `haku-cloud-agent` root is
suspended at its eight-block last-applied configuration; using its current six-block HCL
instead would produce a desired-source total of 274, but would not describe deployed
state.

Since the previous snapshot, retiring the Manifold and PostScanMail OAuth integrations
removed 11 Terraform blocks, including 10 conditional conversion candidates. Adding the
Airlock OIDC provider contributes four current blocks (three Blueprint candidates and
one Secret that depends on the deferred credential bridge). Its session Password block
is already out of Terraform and owned by ESO. This changes the deployed total from 283
to 276 and the conditional candidate estimate from 169 to 163.

These are ratios of Terraform resource blocks, used as a consistent proxy for
configuration bulk. They are not line-count, implementation-effort, or operational-risk
estimates. Every conversion replaces HCL with another declarative resource or controller
configuration; it does not remove the desired application state.

## Per-root estimate

The main candidate column is the 163-block Authentik Blueprint + credential-bridge
ceiling. The bridge is not verified and is parked. ExternalDNS and the two webhook-token
handoffs are shown separately because they require an additional controller or coordinated
credential rotation.

| Terraform root               | Deployed resource blocks | Main candidates | Main ratio | Additional conditional candidates                                         |
| ---------------------------- | -----------------------: | --------------: | ---------: | ------------------------------------------------------------------------- |
| `agent-machine-access`       |                       69 |              61 |      88.4% | —                                                                         |
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
| `haku-cloud-agent`           |                        8 |               0 |         0% | Suspended; current HCL is 6 blocks, last-applied state is 8               |
| `haku-state`                 |                       19 |               2 |      10.5% | +2 after a coordinated Forgejo webhook-token handoff                      |
| `litellm-keys`               |                       20 |               0 |         0% | —                                                                         |
| `sso-providers`              |                       81 |              76 |      93.8% | —                                                                         |
| `thrive-scrape`              |                        7 |               0 |         0% | Unassessed private source; excluded from candidate numerator              |
| **Total**                    |                  **276** |         **163** |  **59.1%** | **+7 ExternalDNS; +4 webhook handoffs**                                   |

If both webhook tokens are rotated and handed to ESO, the purely arithmetic ceiling
becomes 174/276 = 63.0%. I would not plan against that ceiling: both tokens are coupled
to webhook configuration, and the Haku webhook path is derived from its token. Keep
those in Terraform until the webhook can be updated and verified as one controlled
change.

## Main conversion paths

### 1. Authentik objects: Blueprints possible, credential bridge parked

The four roots contain 127 Authentik resource blocks: 54 in `agent-machine-access`,
10 in `alloy-otlp-bearer-token`, 3 in `gatus-sso`, and 60 in `sso-providers`. Five are
`authentik_token` resources whose keys are returned only when created; for example,
[`service_account_claude.tf`](../tf/gitops/sso-providers/service_account_claude.tf)
stores the returned key in a Kubernetes Secret. Keep those five resources and their
five output Secrets in Terraform unless a controller takes over creation and durable
secret capture. The remaining 122 Authentik objects are Blueprint candidates.

This repository already runs Authentik Blueprints. They can own applications,
OAuth/proxy providers, groups, bindings, users, brands, and mappings. Blueprint export
omits write-only fields such as OAuth provider secrets, so it cannot by itself preserve
the existing client Secret delivery to applications.

The live Authentik 2026.8.2 API returned a nonempty `client_secret` for a provider when
queried with the bootstrap admin token; the detail result matched the current Gatus
Secret. The value was not printed. This confirms API readback under that identity only;
least-privilege access and a supported Secret handoff were not verified. The 23
OAuth-linked Secret blocks therefore remain conditional on a bridge that has not been
proven.

Per the current decision, do not pursue the ESO Webhook provider in this phase. Its
earlier server-side schema dry run did not exercise a reconcile or write a Secret. A
purpose-built Authentik controller could be evaluated later if write-through delivery is
still wanted, but its ability to capture the generated secret at create time is also
unverified. Keep these provider Secrets and their ownership in Terraform for now; the
Blueprint conversion estimate is a ceiling, not a ready-to-execute plan.

There are seven other app/session password blocks in the Authentik roots that can be
considered separately for ESO Password. The acceptance operator's Authentik password,
protected Agentplane encryption/signing material, five Authentik token outputs, and
their Secrets remain excluded from the 163-block estimate.

### 2. App-generated passwords and Kubernetes Secrets: ESO

Outside the Authentik roots, 11 blocks are good ESO candidates:

- `budget-ledger`: one random password and its Secret (2/6 blocks).
- `cpap-data`: two random passwords and two Secrets (4/10).
- `forgejo-claude`: one random password and two credential Secrets (3/4).
- `haku-state`: the Haku Console agent API password and Secret (2/19).

Airlock's session-signing key is already on ESO Password plus ExternalSecret; the
Terraform `random_password` was removed and the replacement key was rotated, so existing
browser sessions had to sign in again. Terraform still owns the Authentik client ID and
client secret. The live handoff was verified as synced, with the Airlock Flux
Kustomization healthy and its pod ready. This pilot is complete and is not included in
the remaining 11-block estimate.

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

1. Keep the completed Airlock ESO Password handoff as the reference pattern: explicit
   Secret ownership transfer, a one-time rotation, and no Terraform or ESO interval
   changes.
2. The next isolated pilot is the Agentplane staging session-signing key. Keep the
   testing environment's separate Secret wiring unchanged; the migration only covers
   staging and should leave the Authentik client credentials in Terraform. This rotates
   staging sessions, so existing users will need to sign in again.
3. Apply the same ownership and regeneration safeguards to the remaining simple ESO
   password candidates. Use `removed { lifecycle { destroy = false } }` when Terraform
   must forget a Secret that ESO will retain. Avoid relying on `CreatedOnce` alone to
   preserve a value after deleting and recreating the ExternalSecret.
4. Keep Authentik provider objects and OAuth Secrets in Terraform for now. Revisit
   Blueprints only with a separately proven credential handoff; the ESO Webhook path is
   parked and no Authentik controller is proposed in this phase.
5. Evaluate ExternalDNS separately. Its 2.5% overall reduction may not justify adding
   another controller unless DNS record churn or Terraform reconciliation is an actual
   operational burden.

## Evidence and references

- Live snapshot: 19 Terraform CRs including `infra-drift`; 18 in scope and all 18
  `Ready=True`. The shared GitRepository artifact was `devel@sha1:b0295c537897`; the
  parked `haku-cloud-agent` CR is suspended and retains an eight-block last-applied
  configuration, while its current HCL root has six. The private `thrive-scrape` source
  was `main@sha1:04c4288` and its seven blocks remain unassessed.
- Deployed-revision HCL `resource` block counts, grouped by root and resource type.
  `infra-drift` is excluded by request; `augur-evidence` is absent from the live TF CRs
  and is not counted.
- Repository SSO ownership/secret flow notes: [`cluster/docs/sso.md`](../cluster/docs/sso.md).
- Authentik says write-only OAuth provider secrets are omitted from Blueprint exports:
  [Blueprint export docs](https://docs.goauthentik.io/customize/blueprints/export).
- Authentik API reference lists `client_secret` on OAuth provider retrieve:
  [provider retrieve API](https://docs.goauthentik.io/docs/developer-docs/api/reference/providers-oauth-2-retrieve).
- Live API verification on Authentik 2026.8.2: list filtered by `client_id` returned one
  match and a nonempty secret; detail retrieve matched the live Gatus Secret. Both calls
  used the bootstrap admin token. No value was printed, and no Authentik or Kubernetes
  object was changed.
- The Airlock ESO Secret was synced after the PR #7521 sparse-checkout follow-up; the
  Airlock Flux Kustomization and app pod were Ready. The Agentplane staging change is a
  separate planned pilot and does not alter testing.
- Authentik's OAuth2 provider model generates a secret by default when one is omitted
  ([model source](https://github.com/goauthentik/authentik/blob/main/authentik/providers/oauth2/models.py)).
  The Terraform provider uses the POST response for the provider ID, then performs a
  detail GET to populate `client_secret` ([implementation](https://github.com/goauthentik/terraform-provider-authentik/blob/main/pkg/provider/resource_provider_oauth2.go#L1491-L1542));
  whether the POST response itself includes the secret remains unverified.
- The upstream [Terraform provider OAuth2 implementation](https://github.com/goauthentik/terraform-provider-authentik/blob/main/pkg/provider/resource_provider_oauth2.go#L1510-L1541)
  reads the provider and sets its `client_secret` attribute.
- ESO documents that Password generator invocations create new values and do not retain
  prior outputs: [Generators](https://external-secrets.io/latest/guides/generator/).
  Secret refresh policies and `CreatedOnce` recreation behavior are documented in
  [ExternalSecret](https://external-secrets.io/main/api/externalsecret/).
- ExternalDNS [CRD source](https://kubernetes-sigs.github.io/external-dns/latest/docs/sources/crd/)
  supports declarative `DNSEndpoint` records, including record types such as A, MX, and
  TXT.
