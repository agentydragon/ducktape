"""The Vertical Pod Autoscaler in kube-system, from Fairwinds' chart repository (which the
goldilocks directory's release also uses)."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
)

from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, helm_repository_source_ref, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "vpa"
NAMESPACE = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/vpa"
_REPOSITORY_NAME = "fairwinds-stable"
_REPOSITORY_NAMESPACE = "flux-system"
_ADMISSION_DEPLOYMENT = f"{NAME}-admission-controller"
# Goldilocks installs from this repository too, from its own chart.
REPOSITORY_SOURCE_REF = helm_repository_source_ref(_REPOSITORY_NAME, _REPOSITORY_NAMESPACE)

# The chart's metrics readiness can succeed before TLS is ready. Wait for the admission listener,
# which opens after cache sync and certificate loading.
_ADMISSION_STARTUP_PATCH = f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {_ADMISSION_DEPLOYMENT}
  namespace: {NAMESPACE}
spec:
  template:
    spec:
      containers:
        - name: {NAME}
          startupProbe:
            tcpSocket:
              port: http
            periodSeconds: 5
            failureThreshold: 60
"""


def _component(*, memory_request: str, memory_limit: str, **extra: object) -> dict[str, object]:
    return {
        "enabled": True,
        **extra,
        "resources": {
            "requests": {"cpu": "25m", "memory": memory_request},
            "limits": {"cpu": "200m", "memory": memory_limit},
        },
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(
            chart, _REPOSITORY_NAME, _REPOSITORY_NAMESPACE, url="https://charts.fairwinds.com/stable"
        ),
        chart=NAME,
        version="5.0.1",
        interval="30m",
        chart_interval="12h",
        install=RETRY_FAILED_INSTALL,
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
                                kind="Deployment", name=_ADMISSION_DEPLOYMENT, namespace=NAMESPACE
                            ),
                            patch=_ADMISSION_STARTUP_PATCH,
                        )
                    ]
                )
            )
        ],
        values={
            "recommender": _component(
                memory_request="256Mi",
                memory_limit="512Mi",
                # 15m interval reduces control-plane system-disk writes.
                extraArgs={"recommender-interval": "15m"},
            ),
            "updater": _component(memory_request="128Mi", memory_limit="256Mi"),
            "admissionController": _component(
                memory_request="128Mi",
                memory_limit="256Mi",
                replicaCount=3,
                podDisruptionBudget={"minAvailable": 2},
                affinity={
                    "podAntiAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": [
                            {
                                "labelSelector": {
                                    "matchLabels": {
                                        "app.kubernetes.io/component": "admission-controller",
                                        "app.kubernetes.io/instance": NAME,
                                        "app.kubernetes.io/name": NAME,
                                    }
                                },
                                "topologyKey": "kubernetes.io/hostname",
                            }
                        ]
                    }
                },
            ),
        },
    )
    return chart
