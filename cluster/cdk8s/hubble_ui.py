"""The ingress NetworkPolicy in front of Cilium's Hubble UI.

Hubble UI has no built-in auth -- anyone reaching port 8081 sees all cluster network
flows -- so ingress is restricted to the Authentik proxy outpost (a native blueprint in
`cluster/k8s/authentik/app/blueprints/`) and Gatus. Traffic flows: Gateway ->
ak-outpost-hubble-outpost (auth) -> hubble-ui backend. For direct access:
`kubectl port-forward -n kube-system svc/hubble-ui 8080:80`.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s import cilium

NAME = "hubble-ui"
_NAMESPACE = "kube-system"
# The Service Cilium's Helm chart creates.
URL = f"http://{NAME}.{_NAMESPACE}.svc.cluster.local:80"
_PORT = 8081


def _from(namespace: str, pod_labels: dict[str, str]) -> k8s.NetworkPolicyIngressRule:
    return k8s.NetworkPolicyIngressRule(
        from_=[
            k8s.NetworkPolicyPeer(
                namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": namespace}),
                pod_selector=k8s.LabelSelector(match_labels=pod_labels),
            )
        ],
        ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(_PORT), protocol="TCP")],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNetworkPolicy(
        chart,
        "ingress",
        metadata=k8s.ObjectMeta(name="hubble-ui-ingress", namespace=_NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": NAME}),
            policy_types=["Ingress"],
            ingress=[
                _from("authentik", {"app.kubernetes.io/component": "server", "app.kubernetes.io/name": "authentik"}),
                # Gatus: health check probes
                _from(cilium.PROBER.namespace, cilium.PROBER.selector),
            ],
        ),
    )
    return chart
