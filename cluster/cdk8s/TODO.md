# cdk8s contract backlog

Priority and overall direction: [PLAN.md](PLAN.md). Remaining hand-written manifests,
overlays and application payloads: [remainder backlog](../docs/cdk8s_remainder.md).
Remove entries when their acceptance conditions land; this is not a history of the
conversion.

## CI render diffs if manifests move to a separate branch

Generated manifests are committed alongside cdk8s changes today, so review their PR diff.
If cdk8s source and Flux manifests move to separate branches, consider rendering both
revisions in PR CI and publishing a reviewable resource diff. Prefer the real Flux tools
and controller behavior over a separate approximation; state what the comparison can and
cannot establish about substitutions, secrets, external sources and live readiness.

## ESO store/consumer agreement

The [rendered-tree check](../docs/cdk8s.md#sops-secrets-in-a-converted-directory) includes
local Flux `targetNamespace` overrides and reports rendered consumers it cannot resolve.
External source trees and namespaces or selector labels created outside the render remain
outside its inventory. Keep access grants explicit.

Done: all resolvable consumers are checked and any unresolved cases are described without
claiming the render proves live readiness or maintaining a duplicate provider roster.

## Fleet rule waivers

The active Agentplane egress proxy (`agentplane/generation.py`) is exempt by design:
the interception proxy terminates TLS, so its allowlist is the egress PolicySets.

The parked `github-api-proxy` (`x/github_api_proxy/proxy.py`) also carries a waiver and a
`CLEANUP(added 2026-10-12)` comment. Before revival, pin its `serverNames` or document a
by-design exemption.

Synth fails on a waiver whose object passes, so a fix includes deleting it.

Done: no `CLEANUP(added 2026-10-12)` waiver remains except documented by-design exemptions.

## Fleet rule references

`reference_kind_mismatches` is a same-App kind-mismatch check, not missing-reference
validation. It skips unknown namespaces and suppresses a mismatch when the expected kind
exists anywhere in the App, since external producers may also exist. Whole-tree resolution
must distinguish:

- Namespaced Secret/ConfigMap objects and controller-created targets (ESO, Certificate,
  CNPG, trust-manager), with their actual output semantics.
- SOPS metadata and Kustomize-generated names after composition.
- Optional references and outputs produced outside the rendered tree (Terraform,
  runtime token brokers, Jobs and reflector copies).

Report unresolved external producers as a coverage limit. Do not invent consumer-side
`provides` records or add readiness edges for every reference. The correct boundary is
where the necessary inputs already coexist; a global cdk8s App is not a prerequisite.

Done: precise coverage and diagnostics, with no false promise that synthesis proves
live Secret existence or service readiness.

## Lightweight settings imports

Move remaining deployment schemas and their required submodels out of service runtime
modules, updating all callers. Follow the application-owned
`agentplane/llm_ingress/settings.py` pattern. Inspect transitive imports too: a settings
module importing runtime helpers still pulls those runtimes into synthesis.

Done: synthesis imports the deployment contract without importing service runtimes.
Measure before changing Bazel test sizes; removing an import is not timing evidence.

## Cilium peer references

Repeated endpoint labels should come from the workload owner. Prefer a concrete
construct within a resource chart, or exported labels passed explicitly across charts.
Sharing a label value prevents spelling drift; it does not prove a workload exists or
that policy permits traffic.

Done per neighborhood: producer and consumers share labels without import cycles or a
new peer registry.

## Kyverno CEL policy migration

Kyverno 1.20 stops enforcing `kyverno.io` `ClusterPolicy`/`Policy`/`CleanupPolicy`/
`ClusterCleanupPolicy`; its chart refuses to upgrade while any exist (override
`upgrade.acknowledgeLegacyPoliciesNotEnforced` = they silently stop enforcing). Legacy CRDs
go away in 1.21. The chart is held at 3.9.x (Kyverno 1.19) in `renovate.json5` until done.

Move to `policies.kyverno.io/v1` kinds (CEL; `preconditions` -> `matchConditions`,
`context` -> `spec.variables`, `patchStrategicMerge` -> `ApplyConfiguration`/`JSONPatch`).
Moved: `require-gitops`, `default-disable-service-links`, `require-secret-store-conditions`,
the janitor, and `agentplane-staging-launcher-relay` (`ValidatingPolicy` plus a `MutatingPolicy`, both
with `resource.Get` variables), and the props finished-pod reaper (`NamespacedDeletingPolicy`). Left:
gaffer-private's thrive-scraper Job reaper (agentydragon/gaffer-private#1467), then the chart bump below.

Offline `kyverno apply` evaluates `resource.Get` only for kinds it can map (core kinds with
`--context-file`); for KubeVirt and Agentplane kinds the tests substitute `json.unmarshal`
variables, so the live lookups run only in the cluster.

Done when: no `kyverno.io` policy kinds remain; `providers/kyverno` wrapper targets the new CRDs;
CRD pins in `devinfra/bazel/artifacts.MODULE.bazel` and the `kyverno` CLI pin match 1.20; the
Renovate hold is removed.

## Watch GitHub secret inputs declaratively (deferred)

Keep the current polling and Attic rotator Workspace notification for now.
The deployed provider-opentofu v1.1.10 defaults to a 10-minute poll interval
with plus-or-minus one minute of jitter, so unrelated Secret changes normally
propagate on the next poll, plus OpenTofu execution time.

Before adding production watch infrastructure, evaluate native Secret dependency
watches in provider-opentofu. Its Secret references currently supply values at
reconcile time without triggering reconciliation; dependencies read inside HCL
would need an explicit declaration. A Crossplane required-resource Composition
watch with Workspace UID/resourceVersion annotations is a verified alternative;
see [the spike](../debug/native_secret_watch.md). The production implementation
is deferred by decision on 2026-10-30.

Done: changing or recreating any input Secret promptly reconciles the Workspace,
including changes outside the rotator; retries survive controller restarts, no
credential values are copied into Workspace metadata/spec, and the rotator no
longer needs Workspace patch permission. Keep OpenTofu as the sole GitHub secret
owner and preserve the existing Workspace identity and state during migration.
