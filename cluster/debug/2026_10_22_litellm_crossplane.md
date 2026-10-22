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

This follow-up creates a top-level `ducktape` organization, associates all Keys
through typed references, and reads `attribute.key` from each provider
connection Secret. The user explicitly accepted key rotation and a brief
outage if reassociation requires replacement. After the manifests are
reconciled, verify the Organization, all Team and Key resources become Synced,
exported Secrets become Ready,
`litellm-crossplane-keys` becomes Ready, and then
`litellm-buildbuddy-sync` applies and becomes Ready. Until then, BuildBuddy sync
is blocked on its dependency.
