# Flux Kustomization DAG construction sketch

Design plan only; this file documents the proposed refactor and is not an
implementation patch. The base is the generated Flux Kustomization layer in
#7391 (`flux-kustomizations-all`). The example edge is
`monitoring-crds -> cilium-monitoring`.

## Graph-wide refactor plan

1. Read the existing generated Flux `dependsOn` fields and derive one
   topological order for the current DAG. Keep each dependency's position and
   its nearby explanatory comment.
2. Adapt the existing `flux_kustomization` helper to receive one shared cdk8s
   `Chart` and return the generated Flux `Kustomization` CRD construct. Add
   `flux_kustomization_depends_on(kustomization)` to produce a typed
   `KustomizationSpecDependsOn` from that construct's name and namespace, plus
   `flux_kustomization_depends_on_many(*dependencies)` to return a list of those
   entries for multiple constructs. `flux_kustomization` receives a complete
   spec and passes it to the CRD constructor unchanged.
3. Update each existing per-Kustomization factory in place. Its parameters are
   the shared chart and explicit construct parameters for its generated
   predecessors. Each factory uses
   `flux_kustomization_depends_on_many(predecessor_a, predecessor_b, ...)` for
   multiple generated predecessors, then creates and returns its Flux CRD. Keep
   the existing field values, comments, module locations, and non-Flux
   generation logic.
4. In `generate_manifests.py`, construct one chart and make explicit factory
   calls in topological order, passing predecessor locals directly to
   dependents. Synthesize the chart once and write the resulting manifests to
   their current files. Do not add a runtime graph registry or sorter, lazy
   references, inter-module imports, cdk8s `Node` edges, classes, or helper
   layers.
5. Adapt the existing specialized generators in place. Their Flux factory
   portions take and return the same constructs as the static factories; their
   unrelated resource generation and provider logic stays where it is.

For output routing, the existing `spec.path` identifies the Flux manifest's
current directory for most entries. Preserve the six existing destinations
where it does not: `external-secrets-crds`, `monitoring-crds`, `haku-dispatch`,
`snapshot-controller`, `snapshot-controller-crds`, and `sshpiper-crds`. Keep
those path exceptions explicit; do not introduce a generic path-record type or
writer registry.

### Edges whose target is outside this generated set

Two current dependencies point to Kustomizations that are not generated as
constructs by #7391:

- `artifact-generators -> flux-system`
- `ssh-mcp -> ssh-mcp-namespace`

Keep those as their existing literal typed `KustomizationSpecDependsOn`
entries. There is no target construct to pass through Python. If the intended
scope expands to construct those targets too, stop and ask before expanding it;
do not create placeholders or lazy references.

### Namespace rendering detail

The helper copies the predecessor construct's `metadata.namespace`. Existing
same-namespace dependencies that omitted `namespace` may therefore render with
an explicit namespace. Flux defaults an omitted dependency namespace to the
namespace of the dependent Kustomization, so this can preserve reconciliation
behavior while changing the parsed YAML dict. Report those output diffs
explicitly; do not add per-edge flags or extra metadata to preserve the old
omission spelling without approval.

## Small example

The existing `flux_kustomization` helper would receive the shared `Chart` and
fully constructed spec, then return the cdk8s Flux `Kustomization` CRD
construct. It would not patch or modify `spec`:

```python
from cdk8s import ApiObjectMetadata, Chart, Testing
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

NAMESPACE = "ducktape-flux"


def flux_kustomization_depends_on(
    dependency: Kustomization,
) -> KustomizationSpecDependsOn:
    return KustomizationSpecDependsOn(
        name=dependency.name,
        namespace=dependency.metadata.namespace,
    )


def flux_kustomization_depends_on_many(
    *dependencies: Kustomization,
) -> list[KustomizationSpecDependsOn]:
    return [flux_kustomization_depends_on(dependency) for dependency in dependencies]


def flux_kustomization(
    chart: Chart,
    name: str,
    *,
    spec: KustomizationSpec,
    description: str | None = None,
) -> Kustomization:
    return Kustomization(
        chart,
        name,
        metadata=ApiObjectMetadata(
            name=name,
            namespace=NAMESPACE,
            annotations={"description": description} if description else None,
        ),
        spec=spec,
    )
```

The predecessor factory creates its cdk8s CRD first:

```python
def monitoring_crds(chart: Chart) -> Kustomization:
    return flux_kustomization(
        chart,
        "monitoring-crds",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="1h",
            # The description-bearing variant: the CRDs kube-prometheus-stack's own `crds`
            # subchart installed, so adopting them changes no schema.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="prometheus-operator-source",
                namespace="ducktape-flux",
            ),
            path="./example/prometheus-operator-crd-full",
            prune=False,  # Don't delete CRDs on uninstall (safety)
            wait=True,
            timeout="5m",
        ),
    )
```

The dependent factory receives that actual construct, builds its complete Flux
spec with the small conversion helper, and creates its own CRD:

```python
def cilium_monitoring(
    chart: Chart,
    monitoring_crds: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cilium-monitoring",
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="2m",
            path="./cluster/k8s/monitoring/cilium",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-cilium",
                namespace="ducktape-flux",
            ),
            # ServiceMonitor
            depends_on=[flux_kustomization_depends_on(monitoring_crds)],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1",
                    kind="ServiceMonitor",
                    name="cilium-agent",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1",
                    kind="ServiceMonitor",
                    name="hubble",
                    namespace="monitoring",
                ),
            ],
        ),
    )
```

The central builder makes the topological order and object plumbing visible:

```python
chart = Testing.chart()
monitoring_crds_object = monitoring.monitoring_crds(chart)
cilium_monitoring_object = monitoring.cilium_monitoring(chart, monitoring_crds_object)
manifests = Testing.synth(chart)
```

## Validation plan

- Generate the manifests and compare every old and new Flux Kustomization with
  `yaml.safe_load`; report the count and each difference, including any explicit
  namespace rendering noted above.
- Run `bbr test //cluster/cdk8s:test_generate_manifests` and
  `bbr test //cluster/cdk8s/... //cluster/validation/...`.
- Build all changed `py_library` targets for the mypy aspect.
- Run Gazelle in diff mode and pre-commit over all touched files.

If any typed field cannot be represented, an in-graph dependency cannot be
plumbed directly, or completing this plan appears to require another helper,
class, or indirection, stop and ask before changing the design.
