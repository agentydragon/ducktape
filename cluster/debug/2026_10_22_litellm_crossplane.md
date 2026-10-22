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
  the API omits it. The cdk8s Key spec now sets the optional field to `""`. The
  provider omits empty organization IDs from API requests, while the configured
  value gives Terraform a known result.
- The Antigravity Team spec sent one fallback entry with an empty
  `fallback_models` list. LiteLLM's team-info response omitted that field, but
  the provider schema requires it for every fallback entry, so Observe failed
  while refreshing `router_settings`. The Antigravity lane has no fallbacks;
  the cdk8s spec now omits `router_settings` for that Team. The live Team has no
  external-name or external-create-succeeded annotation, so there is no
  provider-created Team to delete before retrying.

The generated-manifest test passes with these changes. After merge, verify that
all Team and Key resources become Synced, `litellm-crossplane-keys` becomes Ready,
and then `litellm-buildbuddy-sync` applies and becomes Ready. The BuildBuddy
sync has not run yet while its dependency is blocked.
