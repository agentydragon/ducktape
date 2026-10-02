"""VolSync, with the ServiceAccount token its metrics scrape authenticates with.

The chart protects `/metrics` with Kubernetes token auth, but its generated ServiceMonitor
configures no credentials: a post-renderer injects the Secret-backed token, so Alloy does not
need to read a local token file.
"""

from __future__ import annotations

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

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "volsync"
NAMESPACE = "volsync-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/volsync"
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
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.INITIAL)
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
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, "backube", "flux-system", url="https://backube.github.io/helm-charts/"),
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
        values={"manageCRDs": True, "nodeSelector": node_scheduling.HIL_OVH_NODE_SELECTOR},
    )
    return chart


def volsync(chart: Chart, directory: RenderedDirectory, monitoring_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        # The chart renders its ServiceMonitor only if the CRD exists when Helm installs it.
        depends_on=[flux_kustomization_depends_on(monitoring_crds)],
        # The token controller populates data.token asynchronously. Do not declare
        # the VolSync auth material ready until Alloy can actually use it.
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="v1", kind="Secret", current="has(data.token) && data.token != ''"
            )
        ],
    )
