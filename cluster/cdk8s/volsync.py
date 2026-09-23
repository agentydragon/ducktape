"""VolSync, with the ServiceAccount token its metrics scrape authenticates with.

The chart protects `/metrics` with Kubernetes token auth, but its generated ServiceMonitor
configures no credentials: a post-renderer injects the Secret-backed token, so Alloy does not
need to read a local token file.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import helm_release
from cluster.cdk8s.metadata import metadata

NAME = "volsync"
NAMESPACE = "volsync-system"
OUTPUT_DIR = "cluster/k8s/volsync"
_METRICS_ACCOUNT = "volsync-metrics"
_METRICS_TOKEN = "volsync-metrics-token"
_SERVICE_MONITOR_AUTH_PATCH = f"""\
- op: add
  path: /spec/endpoints/0/authorization
  value:
    type: Bearer
    credentials:
      name: {_METRICS_TOKEN}
      key: token
"""


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"goldilocks.fairwinds.com/enabled": "true", "goldilocks.fairwinds.com/vpa-update-mode": "initial"},
        ),
    )
    account = k8s.KubeServiceAccount(
        chart, "metrics-account", metadata=k8s.ObjectMeta(name=_METRICS_ACCOUNT, namespace=NAMESPACE)
    )
    k8s.KubeSecret(
        chart,
        "metrics-token",
        metadata=k8s.ObjectMeta(
            name=_METRICS_TOKEN, namespace=NAMESPACE, annotations={"kubernetes.io/service-account.name": account.name}
        ),
        type="kubernetes.io/service-account-token",
    )
    k8s.KubeClusterRoleBinding(
        chart,
        "metrics-reader-binding",
        metadata=k8s.ObjectMeta(name="volsync-metrics-reader-binding"),
        # The ClusterRole comes with the chart.
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="volsync-metrics-reader"),
        subjects=[k8s.Subject(kind="ServiceAccount", name=account.name, namespace=NAMESPACE)],
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata("backube", "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://backube.github.io/helm-charts/"),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart=NAME,
        version="0.16.0",
        interval="30m",
        chart_interval="12h",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(kind="ServiceMonitor", name=NAME),
                            patch=_SERVICE_MONITOR_AUTH_PATCH,
                        )
                    ]
                )
            )
        ],
        values={"manageCRDs": True, "nodeSelector": {"topology.kubernetes.io/zone": "hil-ovh"}},
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def volsync(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, snapshot_controller: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(snapshot_controller)],
        # The token controller populates data.token asynchronously. Do not declare
        # the VolSync auth material ready until Alloy can actually use it.
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
            )
        ],
    )
