"""Headlamp, the Kubernetes web UI, behind Authentik OIDC, plus the cluster-admin binding for
the operator's OIDC identity it logs in as."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_helm.io.fluxcd.toolkit.helm import HelmReleaseSpecUpgrade, HelmReleaseSpecUpgradeRemediation

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "headlamp"
NAMESPACE = "headlamp"
OUTPUT_DIR = f"{GENERATED_ROOT}/headlamp"
# The chart's Service.
URL = f"http://{NAME}.{NAMESPACE}.svc.cluster.local:80"
_PLUGINS_CONFIG = """\
plugins:
  - name: headlamp_flux
    source: https://artifacthub.io/packages/headlamp/headlamp-plugins/headlamp_flux
    version: "0.7.0"
  - name: headlamp_cert-manager
    source: https://artifacthub.io/packages/headlamp/headlamp-plugins/headlamp_cert-manager
    version: "0.1.1"
  - name: headlamp_kyverno
    source: https://artifacthub.io/packages/headlamp/headlamp-plugins/headlamp_kyverno
    version: "0.1.0"
  - name: headlamp_keda
    source: https://artifacthub.io/packages/headlamp/headlamp-plugins/headlamp_keda
    version: "0.1.2"
  - name: headlamp_kubevirt
    source: https://artifacthub.io/packages/headlamp/headlamp-kubevirt/headlamp_kubevirt
    version: "0.3.1"
installOptions:
  parallel: true
  maxConcurrent: 2
"""


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO, agent_readable=None)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://kubernetes-sigs.github.io/headlamp/"),
        chart=NAME,
        # 0.45.0 ships the Prometheus details-view plugin, enabled by default.
        # renovate: datasource=helm depName=headlamp registryUrl=https://kubernetes-sigs.github.io/headlamp/
        version="0.45.0",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        upgrade=HelmReleaseSpecUpgrade(remediation=HelmReleaseSpecUpgradeRemediation(retries=3)),
        values={
            "replicaCount": 1,
            "config": {
                # OIDC mode: Headlamp redirects to Authentik, JWT forwarded to K8s API server
                # which validates it via oidc-issuer-url (in Talos machine config).
                # Each user gets their own K8s identity (oidc:<username>).
                "oidc": {
                    "secret": {"create": False},
                    "externalSecret": {"enabled": True, "name": "headlamp-oidc-secret"},
                },
                "watchPlugins": True,
            },
            "httpRoute": {
                "enabled": True,
                "parentRefs": [{"name": "cluster-gateway", "namespace": "gateway-system"}],
                "hostnames": ["headlamp.allegedly.works"],
            },
            "ingress": {"enabled": False},
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "pluginsManager": {"enabled": True, "version": "0.1.1", "configContent": _PLUGINS_CONFIG},
        },
    )
    k8s.KubeClusterRoleBinding(
        chart,
        "oidc-agentydragon-admin",
        metadata=k8s.ObjectMeta(name="oidc-agentydragon-admin"),
        subjects=[k8s.Subject(kind="User", name="oidc:agentydragon", api_group="rbac.authorization.k8s.io")],
        role_ref=k8s.RoleRef(kind="ClusterRole", name="cluster-admin", api_group="rbac.authorization.k8s.io"),
    )
    return chart


def headlamp(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="10m")
