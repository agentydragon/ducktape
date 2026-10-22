# LiteLLM Crossplane first reconciliation

The Crossplane migration PR and Tofu handoff are merged, but the live migration is
not healthy yet. `litellm-keys-tf` is Ready; `litellm-crossplane-keys` is still
Progressing, and `litellm-buildbuddy-sync` is blocked on that Kustomization.

The installed `provider-litellm:v2.1.0` reports two independent failures:

- Each Key create that reached the provider returned an unknown
  `organization_id`, which Terraform rejects after apply. Subsequent Observe
  attempts then plan to destroy the key and stop at the provider's
  `prevent_destroy` guard. In the provider's `readKeyWithTransport`,
  `organization_id` is only assigned when LiteLLM returns a non-empty string;
  unlike some neighboring optional fields, an unknown value is not cleared when
  the API omits it. Setting this optional field to `""` does not fix that
  provider behavior: Crossplane v2.4.0's generated reference resolver
  normalizes the empty string to nil, sends `organizationId: null`, and the API
  server rejects that patch because the CRD schema requires a string. The
  recovery manifest now creates one LiteLLM Organization and uses the typed
  `organizationIdRef` on every Key, so LiteLLM returns a concrete organization
  ID. Provider PR [#1](https://github.com/TheCodingSheikh/provider-litellm/pull/1)
  remains a general compatibility fix for keys that intentionally omit an
  organization; it is no longer required for this migration's keys.
- The Antigravity Team spec sent one fallback entry with an empty
  `fallback_models` list. LiteLLM's team-info response omitted that field, but
  the provider schema requires it for every fallback entry, so Observe failed
  while refreshing `router_settings`. The Antigravity lane has no fallbacks;
  the cdk8s spec now omits `router_settings` for that Team. At the first
  observation, the live Team had no external name or create-succeeded
  annotation, so retrying did not require deleting a provider-created Team.

At 2026-10-22 08:04 UTC, Flux was on a new 20-minute health-check cycle for
artifact `latest@sha256:d44f154c9f758e8127ca60126d22500a8b37bfc06c56cb3858ce5f3e996e529b`.
All twelve Keys reported `Synced=False`, with recent events showing the
`organizationId: null` patch rejection. Eight ExternalSecrets were unready
because the generated Crossplane connection Secrets expose `attribute.key`,
while ESO requested a property named `key`. The Team fallback fix had already
been applied, and a later event confirmed the provider requested Team creation.

The first recovery attempt created a top-level `ducktape` organization and
associated every Key through typed references. A live in-pod probe has since
shown that this route is unavailable on the deployed LiteLLM license:
`GET /organization/info?organization_id=ducktape` returns HTTP 403 with the
detail that Organizations are only available to LiteLLM Enterprise users. The
running LiteLLM process also reports `LITELLM_LICENSE` unset. The organization
resource therefore cannot become usable here, regardless of key rotation.

The keys must remain at the root level. The currently installed
`provider-litellm:v2.1.0` has a separate compatibility bug for that shape: its
read path leaves computed `organization_id` unknown when LiteLLM omits it.
Provider PR [#1](https://github.com/TheCodingSheikh/provider-litellm/pull/1)
adds the required unknown-to-null resolution and a regression test; it is still
open. Remove the Organization resource and Key references after a provider
package containing that fix is available and pinned. Key rotation is acceptable
if reconciliation requires it, though retaining the existing keys is
preferable if the provider can observe them in place.

At the latest live check, `litellm-crossplane-keys` is Ready and has applied the
current artifact, but the Organization is Synced=False and all twelve Keys are
Synced=False. `litellm-buildbuddy-sync` is still Progressing. A successful
Kustomization apply therefore does not mean the key migration or BuildBuddy
sync is healthy; confirm all Key resources and exported Secrets, then confirm
the BuildBuddy Terraform reconciliation after the provider fix lands.
