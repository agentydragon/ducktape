"""haku-sandbox, Haku's compute namespace, and its one ingress boundary."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cilium
from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "haku-namespace"
NAMESPACE = "haku-sandbox"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/haku/namespace"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                "goldilocks.fairwinds.com/vpa-update-mode": "auto",
                "name": NAMESPACE,
                # Pin the cluster-default (Talos) baseline profile explicitly. The Haku egress fence
                # depends on it: baseline forbids hostNetwork/hostPort/host*, which is what stops a
                # pod (Haku has full CRUD here) from escaping the namespace-scoped force-proxy CCNP
                # via the host netns. Making it a visible label documents that load-bearing
                # assumption and stops it silently drifting to privileged. See haku/workspaces.py.
                "pod-security.kubernetes.io/enforce": "baseline",
            },
        ),
    )
    # One ingress boundary for the whole namespace: haku-sandbox is a single trust domain (Haku's
    # code at Haku's privilege -- operator ruling 2026-07-30), and the only way in from outside is
    # the Authentik proxy outpost. Selecting every endpoint flips the namespace to default-deny
    # ingress; the union of allows below is exactly {same-namespace, outpost}.
    #
    # What it protects: cross-namespace forgery of the X-authentik-username header haku-ui trusts,
    # and the jupyter sidecar, an unauthenticated exec listener whose only legitimate caller is
    # haku-ui's /jupyter/* reverse proxy (same namespace). In-namespace header forgery is a
    # non-threat -- same actor, nothing to gain over its own git credential.
    #
    # Kubelet probes and operator `kubectl port-forward` arrive via the host entity, which Cilium
    # admits independently of these rules. A future cross-namespace consumer = one explicit
    # fromEndpoints entry here.
    #
    # Egress is unchanged -- the mitmproxy CCNP owns that direction. Operator-owned (Haku has no
    # netpol RBAC). Security model: haku/docs/security.md.
    cilium.network_policy(
        chart,
        "ingress",
        metadata=metadata("haku-sandbox-ingress", NAMESPACE),
        selector=CiliumNetworkPolicySpecEndpointSelector(),
        ingress=[
            CiliumNetworkPolicySpecIngress(
                from_endpoints=[
                    CiliumNetworkPolicySpecIngressFromEndpoints(
                        match_labels={"k8s:io.kubernetes.pod.namespace": NAMESPACE}
                    ),
                    CiliumNetworkPolicySpecIngressFromEndpoints(
                        match_labels={
                            "k8s:io.kubernetes.pod.namespace": "authentik",
                            "app.kubernetes.io/name": "authentik",
                        }
                    ),
                ]
            )
        ],
    )
    return chart


def haku_namespace(flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, root: Path) -> Kustomization:
    write_charts(root, OUTPUT_DIR, chart)
    return flux_kustomization(flux_chart, NAME, artifact, retry_interval=None, wait=None, timeout="2m")
