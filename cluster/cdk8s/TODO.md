# cdk8s contract backlog

Priority and overall direction: [PLAN.md](PLAN.md). Remaining hand-written manifests,
overlays and application payloads: [remainder backlog](../docs/cdk8s_remainder.md).
Remove entries when their acceptance conditions land; this is not a history of the
conversion.

## ESO store/consumer agreement

`cluster/validation/checks.py` sees generated and hand-written resources together.
Resolve each ExternalSecret's store, including ClusterExternalSecret target namespaces;
check store namespace conditions and the ServiceAccount required by Kubernetes-provider
referent authentication. Existing checks cover external credential ownership and the
Forgejo image distribution convention, not this general relationship.

The missing agreement has already caused denied Airlock/Forgejo-image consumers and a
missing `external-creds-reader` ServiceAccount. Handle namespace selectors as well as
literal lists where used. Derive paired fields from an explicitly approved grant when
they have one owner; never grant access just because an ExternalSecret requests it.

Done: synthetic cases catch denied namespaces and absent referent identities, while
valid generated and hand-written consumers pass. No duplicate consumer/provider roster.

## Fleet validation coverage and references

`fleet_rules.py` is attached explicitly by selected charts. Establish which workload
charts it covers, which checks apply to the rest, and which exceptions reflect real
policy. Expanding hardening coverage can change workload behavior and belongs in its
own reviewed change.

`resolved_references` is a same-chart kind-mismatch check, not missing-reference
validation; it currently ignores namespace. Narrow its name/claims or strengthen its
implementation. Whole-tree resolution must distinguish:

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

Agentplane constructs import Settings from `agentplane/{app,egress,llm_ingress,
action_service}/main.py`; aiquota imports `aiquota/api.py`. Move schema definitions
and their required submodels into application-owned modules, updating all callers.
Use the same approach for Airlock/rotators when converting their config.

Done: synthesis imports the deployment contract without importing service runtimes.
Measure before changing Bazel test sizes; removing an import is not timing evidence.

## Cilium peer references

Repeated endpoint labels should come from the workload owner. Prefer a concrete
construct within a resource chart, or exported labels passed explicitly across charts.
Sharing a label value prevents spelling drift; it does not prove a workload exists or
that policy permits traffic.

Done per neighborhood: producer and consumers share labels without import cycles or a
new peer registry.

## Kyverno preconditions typed schema (v2beta1)

`providers/kyverno/cluster_policy.py`'s `Validate.deny(conditions=...)` takes
`preconditions`/`deny.conditions` as a raw dict because `ClusterPolicy`'s `v1` schema --
the version `cdk8s_import` names plainly, and what this repo's `ClusterPolicySpecRules`
types come from -- declares `preconditions` as `x-kubernetes-preserve-unknown-fields:
true`, with no structure at all.

The CRD's `v2beta1` schema (served, not storage; generated as suffixed
`ClusterPolicyV2Beta1Spec*` types) has a real typed shape instead: `any`/`all` arrays of
`{key, operator, value, message}`, with `operator` a genuine 14-value enum. Migrating
the wrapper to `v2beta1` would let `Validate.deny()` grow a typed `preconditions`
factory instead of a raw-dict escape hatch -- worth doing sometime, not urgent.

Done: `providers/kyverno` targets `v2beta1` (or offers it alongside `v1`), and
`preconditions` has a typed factory built from the real `any`/`all` shape.

## Haku setup-script contract

`test_haku_sandbox_setup.py` checks the generated SandboxTemplate's environment against
the image's `haku-sandbox-setup.sh`. The script has no Settings contract; the similarly
named `haku/runtime/agent/config.py` configures a different binary.

Choose a small script-owned input contract or a Python bootstrap with Settings before
retiring this real cross-artifact test. Keep the image/runtime package independent of
`cluster/`.

## Extract only when another caller needs it

The Agentplane sandbox egress fence combines sidecar, token volume, CA mount, routing
environment and policy labels. Extract their composition when a second exec-target
template needs it; do not build a generic workload framework for the existing caller.

A second Haku deployment may justify Environment props. Namespace-default charts may
help a purely namespaced component, but splitting mixed-scope charts solely to omit
`metadata(..., namespace)` is not scheduled.

## Parked

`haku/x/dispatch` still duplicates its model roster between `zones.yaml` and its
workers-LiteLLM generator. Derive both when the package is revived. Other project-owned
raw deployment packages are listed in the remainder backlog; file presence does not
establish live deployment.
