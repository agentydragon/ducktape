# Flux Kustomization DAG construction sketch

Design sketch only; this is not an implementation patch. It uses the existing
`monitoring-crds -> cilium-monitoring` edge as an example.

The existing `flux_kustomization` helper would receive a shared `Chart` and the
fully constructed spec, then return the cdk8s Flux `Kustomization` CRD construct.
It would not patch or modify `spec`:

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

The dependent factory receives that actual object and constructs the complete Flux
spec before constructing its own CRD. The small helper turns the object into the
name-and-namespace reference required by the Flux API:

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

The central builder makes the topological order and direct object plumbing visible:

```python
chart = Testing.chart()
monitoring_crds_object = monitoring_crds(chart)
cilium_monitoring_object = cilium_monitoring(chart, monitoring_crds_object)
manifests = Testing.synth(chart)
```

`cilium_monitoring` passes the predecessor construct through the helper to produce
the Flux API's `name` and `namespace` reference. The Flux spec is complete when its
CRD construct is created. The builder calls factories in topological order and
synthesizes the shared chart once. There is no `node.add_dependency` call: Flux's
`spec.dependsOn` controls Flux reconciliation, and the cdk8s manifest-order edge is
unnecessary for this design.
