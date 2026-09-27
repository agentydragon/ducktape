"""The Talos Cloud Controller Manager's HelmRepository and HelmRelease.

`helmrelease.k8s.yaml` holds only the HelmRelease because `cluster/terraform/main/talos-ccm.tf`
`yamldecode`s it to render the same chart and values as the bootstrap inline manifest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec, HelmRepositorySpecType

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository_source_ref
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "talos-cloud-controller-manager"
NAMESPACE = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/talos-cloud-controller-manager"
_REPOSITORY = "siderolabs"
_PORT = 50258


def helmrepository_chart(app: App) -> Chart:
    chart = Chart(app, "helmrepository", disable_resource_name_hashes=True)
    HelmRepository(
        chart,
        "repository",
        metadata=ApiObjectMetadata(name=_REPOSITORY, namespace=NAMESPACE),
        spec=HelmRepositorySpec(interval="24h", type=HelmRepositorySpecType.OCI, url="oci://ghcr.io/siderolabs/charts"),
    )
    return chart


def helmrelease_chart(app: App) -> Chart:
    chart = Chart(app, "helmrelease", disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=helm_repository_source_ref(_REPOSITORY, NAMESPACE),
        chart=NAME,
        # Pinned: the inline tofu render reads this version too, so Flux + bootstrap
        # stay reproducible and a chart bump can't silently move defaults.
        version="0.5.7",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
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
                {"key": "node.cloudprovider.kubernetes.io/uninitialized", "operator": "Exists", "effect": "NoSchedule"},
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
    )
    return chart


def talos_cloud_controller_manager(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, interval="30m", target_namespace=NAMESPACE)
