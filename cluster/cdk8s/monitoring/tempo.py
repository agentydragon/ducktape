"""Tempo (traces) with its tenant-local SeaweedFS bucket and credentials."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from seaweed_bucket_crds.com.seaweedfs.seaweed import (
    Bucket,
    BucketSpec,
    BucketSpecAccess,
    BucketSpecAccessActions,
    BucketSpecClusterRef,
    BucketSpecReclaimPolicy,
)
from seaweed_resourcereferencegrant_crds.com.seaweedfs.seaweed import (
    ResourceReferenceGrant,
    ResourceReferenceGrantSpec,
    ResourceReferenceGrantSpecFrom,
    ResourceReferenceGrantSpecTo,
)
from seaweed_s3credentials_crds.com.seaweedfs.seaweed import (
    S3Credentials,
    S3CredentialsSpec,
    S3CredentialsSpecIdentityRef,
    S3CredentialsSpecReclaimPolicy,
    S3CredentialsSpecSeaweedRef,
    S3CredentialsSpecSecretRef,
)
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "tempo"
OUTPUT_DIR = "cluster/k8s/monitoring/tempo"
_NAMESPACE = "monitoring"
_SEAWEEDFS = "seaweedfs"
_SEAWEED_GROUP = "seaweed.seaweedfs.com"
_CREDENTIALS_SECRET = "tempo-seaweedfs-credentials"


def _storage(chart: Chart) -> None:
    Bucket(
        chart,
        "bucket",
        metadata=metadata(
            NAME, _NAMESPACE, annotations={"description": "Tempo's tenant-local SeaweedFS trace bucket."}
        ),
        spec=BucketSpec(
            name=NAME,
            adopt_existing=True,
            cluster_ref=BucketSpecClusterRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            reclaim_policy=BucketSpecReclaimPolicy.RETAIN,
            access=[
                BucketSpecAccess(
                    user=NAME,
                    actions=[
                        BucketSpecAccessActions.READ,
                        BucketSpecAccessActions.WRITE,
                        BucketSpecAccessActions.LIST,
                        BucketSpecAccessActions.TAGGING,
                    ],
                )
            ],
        ),
    )
    S3Credentials(
        chart,
        "credentials",
        metadata=metadata(NAME, _NAMESPACE, annotations={"description": "Tempo's tenant-local SeaweedFS credentials."}),
        spec=S3CredentialsSpec(
            seaweed_ref=S3CredentialsSpecSeaweedRef(name=_SEAWEEDFS, namespace=_SEAWEEDFS),
            identity_ref=S3CredentialsSpecIdentityRef(name=NAME),
            secret_ref=S3CredentialsSpecSecretRef(
                name=_CREDENTIALS_SECRET, access_key_field="AWS_ACCESS_KEY_ID", secret_key_field="AWS_SECRET_ACCESS_KEY"
            ),
            reclaim_policy=S3CredentialsSpecReclaimPolicy.RETAIN,
        ),
    )
    # Permit only Tempo's tenant-local Bucket and S3Credentials to reference the
    # SeaweedFS cluster in its namespace.
    ResourceReferenceGrant(
        chart,
        "seaweed-grant",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=ResourceReferenceGrantSpec(
            from_=[
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="Bucket", namespace=_NAMESPACE),
                ResourceReferenceGrantSpecFrom(group=_SEAWEED_GROUP, kind="S3Credentials", namespace=_NAMESPACE),
            ],
            to=[ResourceReferenceGrantSpecTo(group=_SEAWEED_GROUP, kind="Seaweed", name=_SEAWEEDFS)],
        ),
    )
    # Retain the previous credential Secret during the staged handoff. The old
    # S3Credentials resource is retired separately; revoking its retained key and
    # removing this rollback Secret is an explicit follow-up.
    k8s.KubeSecret(
        chart,
        "legacy-credentials",
        metadata=k8s.ObjectMeta(
            name="tempo-s3-credentials", namespace=_NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"}
        ),
        type="Opaque",
    )
    S3Identity(
        chart,
        "identity",
        metadata=metadata(NAME, _SEAWEEDFS),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=_SEAWEEDFS), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    HelmRelease(
        chart,
        "helm-release",
        metadata=metadata(NAME, _NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    version="1.x",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name="grafana",
                        namespace="flux-system",
                    ),
                    interval="12h",
                )
            ),
            values={
                "tempo": {
                    "receivers": {
                        "otlp": {
                            "protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}, "http": {"endpoint": "0.0.0.0:4318"}}
                        }
                    },
                    "storage": {
                        "trace": {
                            "backend": "s3",
                            "s3": {"endpoint": "seaweedfs-s3.seaweedfs.svc:8333", "bucket": NAME, "insecure": True},
                        }
                    },
                    # Metrics-generator: powers TraceQL metrics queries (rate(), count_over_time(), etc.)
                    # in Grafana's Explore Traces app. Without this, queries over "recent" (not yet
                    # flushed to object storage) data fail with:
                    #   error finding generators in Querier.queryRangeRecent: error finding generators: empty ring
                    # because there are no metrics-generator instances registered in the ring.
                    "metricsGenerator": {
                        "enabled": True,
                        "remoteWriteUrl": "http://mimir-gateway.monitoring.svc.cluster.local/api/v1/push",
                        "processor": {"local_blocks": {}, "service_graphs": {}, "span_metrics": {}},
                    },
                    "overrides": {
                        "defaults": {
                            "metrics_generator": {"processors": ["local-blocks", "service-graphs", "span-metrics"]}
                        }
                    },
                    "extraEnvFrom": [{"secretRef": {"name": _CREDENTIALS_SECRET}}],
                },
                "persistence": {"enabled": False},
                "nodeSelector": {"topology.kubernetes.io/region": "hil"},
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
                "serviceMonitor": {"enabled": True},
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def tempo(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    monitoring_crds: Kustomization,
    grafana_helmrepository: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "tempo",
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
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
            depends_on=flux_kustomization_depends_on_many(
                # the chart's serviceMonitor.enabled
                monitoring_crds,
                grafana_helmrepository,
                seaweedfs_cluster,
            ),
        ),
    )
