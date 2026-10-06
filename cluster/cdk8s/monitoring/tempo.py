"""Tempo (traces) with its tenant-local SeaweedFS `PrivateBucket`."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import grafana_helmrepository, mimir
from cluster.cdk8s.seaweedfs import s3

NAME = "tempo"
OUTPUT_DIR = f"{GENERATED_ROOT}/monitoring/tempo"
_NAMESPACE = "monitoring"
_CREDENTIALS_SECRET = "tempo-seaweedfs-credentials"
# The chart's Service exposes OTLP/gRPC on the receiver's port.
_OTLP_GRPC_PORT = 4317
OTLP_GRPC_ENDPOINT = f"{NAME}.{_NAMESPACE}.svc.cluster.local:{_OTLP_GRPC_PORT}"


def _storage(chart: Chart) -> None:
    s3.PrivateBucket(
        chart,
        "storage",
        name=NAME,
        tenant=_NAMESPACE,
        adopt_existing=True,
        description="Tempo's tenant-local SeaweedFS trace bucket.",
        # Not the default `tempo-s3-credentials`: that is the legacy Secret below.
        secret_name=_CREDENTIALS_SECRET,
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


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _storage(chart)
    helm_release(
        chart,
        NAME,
        _NAMESPACE,
        repository=grafana_helmrepository.SOURCE_REF,
        chart=NAME,
        version="1.x",
        interval="30m",
        chart_interval="12h",
        values={
            "tempo": {
                "receivers": {
                    "otlp": {
                        "protocols": {
                            "grpc": {"endpoint": f"0.0.0.0:{_OTLP_GRPC_PORT}"},
                            "http": {"endpoint": "0.0.0.0:4318"},
                        }
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
                    "remoteWriteUrl": mimir.PUSH_URL,
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
            "serviceMonitor": {"enabled": True},
        },
    )
    return chart


def tempo(
    chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "tempo",
        directory,
        wait=None,
        suspend=False,
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
            seaweedfs_operator,
        ),
    )
