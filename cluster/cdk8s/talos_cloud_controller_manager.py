"""The Talos Cloud Controller Manager's HelmRepository and HelmRelease.

`helmrelease.k8s.yaml` holds only the HelmRelease because `cluster/terraform/main/talos-ccm.tf`
`yamldecode`s it to render the same chart and values as the bootstrap inline manifest.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "talos-cloud-controller-manager"
NAMESPACE = "kube-system"
OUTPUT_DIR = "cluster/k8s/talos-cloud-controller-manager"
_REPOSITORY = "siderolabs"
_PORT = 50258


def helmrepository_chart(app: App) -> Chart:
    chart = Chart(app, "helmrepository", disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "repository",
        metadata=metadata(_REPOSITORY, NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", type=HelmRepositorySpecType.OCI, url="oci://ghcr.io/siderolabs/charts"),
    )
    return chart


def helmrelease_chart(app: App) -> Chart:
    chart = Chart(app, "helmrelease", disable_resource_name_hashes=True)
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="15m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    # Pinned: the inline tofu render reads this version too, so Flux + bootstrap
                    # stay reproducible and a chart bump can't silently move defaults.
                    version="0.5.7",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=_REPOSITORY,
                        namespace=NAMESPACE,
                    ),
                )
            ),
            values={
                # Pin the CCM metrics/secure port explicitly. The chart's default is not stable
                # across versions (0.5.4's values.yaml defaults to 10458, but the cluster is on
                # 50258); pinning it here keeps Flux, the inline bootstrap manifest, and the live
                # deployment all on one value, so there is no perpetual drift. 50258 = the value
                # already applied (zero churn).
                "service": {"port": _PORT, "containerPort": _PORT},
                "enabledControllers": ["cloud-node", "cloud-node-lifecycle", "node-csr-approval"],
                "tolerations": [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                    {
                        "key": "node.cloudprovider.kubernetes.io/uninitialized",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    },
                ],
                "transformations": [
                    {
                        "name": "ovh-nodes",
                        "nodeSelector": [
                            {
                                "matchExpressions": [
                                    {"key": "topology.kubernetes.io/region", "operator": "In", "values": ["hil"]}
                                ]
                            }
                        ],
                        "features": {"publicIPDiscovery": True},
                    }
                ],
            },
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, helmrepository_chart, helmrelease_chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["helmrepository.k8s.yaml", "helmrelease.k8s.yaml"]),
    )


def talos_cloud_controller_manager(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, interval="30m", target_namespace=NAMESPACE)
