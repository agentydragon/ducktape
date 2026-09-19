"""Generated Flux Kustomizations for the monitoring slice."""

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def alloy_otlp_bearer_token_tf() -> dict[str, object]:
    return flux_kustomization(
        "alloy-otlp-bearer-token-tf",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/alloy-otlp-bearer-token-tf",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-alloy-otlp-bearer-token-tf",
                namespace="ducktape-flux",
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="alloy-otlp-bearer-token",
                    namespace="flux-system",
                )
            ],
            timeout="10m",
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                # authentik-jwt-rotation owns the agents-infra namespace this secret's
                # rotator runs in, and rotates the alloy-otlp bearer token committed here.
                KustomizationSpecDependsOn(name="authentik-jwt-rotation", namespace="ducktape-flux"),
            ],
        ),
    )


def alloy() -> dict[str, object]:
    return flux_kustomization(
        "alloy",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/alloy",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-alloy",
                namespace="ducktape-flux",
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="alloy", namespace="monitoring"
                )
            ],
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="mimir", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grafana-helmrepository", namespace="ducktape-flux"),
            ],
        ),
    )


def cilium_monitoring() -> dict[str, object]:
    return flux_kustomization(
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
            depends_on=[
                # ServiceMonitor
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux")
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1",
                    kind="ServiceMonitor",
                    name="cilium-agent",
                    namespace="monitoring",
                ),
                KustomizationSpecHealthChecks(
                    api_version="monitoring.coreos.com/v1", kind="ServiceMonitor", name="hubble", namespace="monitoring"
                ),
            ],
        ),
    )


def monitoring_crds() -> dict[str, object]:
    return flux_kustomization(
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


def grafana_db() -> dict[str, object]:
    return flux_kustomization(
        "grafana-db",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/grafana-db",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-grafana-db",
                namespace="ducktape-flux",
            ),
            timeout="5m",
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="postgresql.cnpg.io/v1", kind="Cluster", name="grafana-db-ovh", namespace="monitoring"
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="monitoring-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
            ],
        ),
    )


def grafana_helmrepository() -> dict[str, object]:
    return flux_kustomization(
        "grafana-helmrepository",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/grafana-helmrepository",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="grafana-helmrepository",
                namespace="ducktape-flux",
            ),
            wait=True,
            timeout="5m",
        ),
    )


def grafana_instance() -> dict[str, object]:
    return flux_kustomization(
        "grafana-instance",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/grafana-instance",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="grafana-instance",
                namespace="ducktape-flux",
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="grafana-deployment", namespace="monitoring"
                )
            ],
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="grafana-operator", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grafana-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="sso-providers-tf", namespace="ducktape-flux"),
            ],
        ),
    )


def grafana_operator() -> dict[str, object]:
    return flux_kustomization(
        "grafana-operator",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/grafana-operator",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="grafana-operator",
                namespace="ducktape-flux",
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="grafana-operator",
                    namespace="monitoring",
                )
            ],
            timeout="5m",
            depends_on=[KustomizationSpecDependsOn(name="monitoring-namespace", namespace="ducktape-flux")],
        ),
    )


def loki() -> dict[str, object]:
    return flux_kustomization(
        "loki",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/loki",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name="monitoring-loki", namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="loki", namespace="loki"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="loki", namespace="loki"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="loki", namespace="loki"
                ),
            ],
            timeout="10m",
            depends_on=[
                KustomizationSpecDependsOn(name="grafana-helmrepository", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def mimir() -> dict[str, object]:
    return flux_kustomization(
        "mimir",
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/mimir",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-mimir",
                namespace="ducktape-flux",
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="mimir-blocks", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="mimir-ruler", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="mimir", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="mimir", namespace="monitoring"
                ),
            ],
            timeout="10m",
            depends_on=[
                # the chart's metaMonitoring.serviceMonitor
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grafana-helmrepository", namespace="ducktape-flux"),
                # seaweedfs-cluster provides the Seaweed CR + Bucket CRD that our
                # mimir-blocks / mimir-ruler Bucket resources reference (buckets.yaml).
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def monitoring_namespace() -> dict[str, object]:
    return flux_kustomization(
        "monitoring-namespace",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-namespace",
                namespace="ducktape-flux",
            ),
            wait=True,
            # Health check ensures monitoring namespace exists before dependents deploy
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="monitoring")],
            depends_on=[],
        ),
    )


def monitoring_rules() -> dict[str, object]:
    return flux_kustomization(
        "monitoring-rules",
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/monitoring/rules",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-rules",
                namespace="ducktape-flux",
            ),
            depends_on=[
                # PrometheusRule
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux")
            ],
        ),
    )


def monitoring_stack() -> dict[str, object]:
    return flux_kustomization(
        "monitoring-stack",
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/stack",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-stack",
                namespace="ducktape-flux",
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="v1", kind="Secret", name="alloy-control-plane-token", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2",
                    kind="HelmRelease",
                    name="kube-prometheus-stack",
                    namespace="monitoring",
                ),
            ],
            # The built-in Secret health check only checks existence. This CEL check waits
            # for the service-account token controller to populate data.token.
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
                )
            ],
            timeout="10m",
            depends_on=[
                KustomizationSpecDependsOn(name="monitoring-namespace", namespace="ducktape-flux"),
                # The chart's Prometheus/Alertmanager CRs are rejected at admission until
                # the CRDs exist, and the chart no longer installs them itself.
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="ntfy", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
            ],
        ),
    )


def tempo() -> dict[str, object]:
    return flux_kustomization(
        "tempo",
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/monitoring/tempo",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
                name="monitoring-tempo",
                namespace="ducktape-flux",
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="Bucket", name="tempo", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1", kind="S3Credentials", name="tempo", namespace="monitoring"
                ),
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name="tempo", namespace="monitoring"
                ),
            ],
            timeout="5m",
            depends_on=[
                # the chart's serviceMonitor.enabled
                KustomizationSpecDependsOn(name="monitoring-crds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="grafana-helmrepository", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    output_dir = root / "cluster/k8s/monitoring/alloy-otlp-bearer-token-tf"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", alloy_otlp_bearer_token_tf())

    output_dir = root / "cluster/k8s/monitoring/alloy"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", alloy())

    output_dir = root / "cluster/k8s/monitoring/cilium"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", cilium_monitoring())

    output_dir = root / "cluster/k8s/monitoring/crds"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", monitoring_crds())

    output_dir = root / "cluster/k8s/monitoring/grafana-db"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", grafana_db())

    output_dir = root / "cluster/k8s/monitoring/grafana-helmrepository"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", grafana_helmrepository())

    output_dir = root / "cluster/k8s/monitoring/grafana-instance"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", grafana_instance())

    output_dir = root / "cluster/k8s/monitoring/grafana-operator"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", grafana_operator())

    output_dir = root / "cluster/k8s/monitoring/loki"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", loki())

    output_dir = root / "cluster/k8s/monitoring/mimir"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", mimir())

    output_dir = root / "cluster/k8s/monitoring/namespace"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", monitoring_namespace())

    output_dir = root / "cluster/k8s/monitoring/rules"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", monitoring_rules())

    output_dir = root / "cluster/k8s/monitoring/stack"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", monitoring_stack())

    output_dir = root / "cluster/k8s/monitoring/tempo"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(output_dir / "flux-kustomization.yaml", tempo())
