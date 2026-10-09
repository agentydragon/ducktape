"""This cluster's own Cilium policy facts and recipes, layered on the generic wrappers in
`cluster.cdk8s.providers.cilium`: which labels reach this cluster's kube-dns and Authentik, which
Pods scrape metrics and probe uptime, the node-IP/SNI workaround this cluster's hostNetwork
Gateway needs for egress to its own public hostnames, and the clusterwide policy forcing a
sandbox namespace's egress through its proxy.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from cilium_clusterwide_crds.io.cilium import (
    CiliumClusterwideNetworkPolicySpecEgress,
    CiliumClusterwideNetworkPolicySpecEgressToEndpoints,
    CiliumClusterwideNetworkPolicySpecEgressToEntities,
    CiliumClusterwideNetworkPolicySpecEgressToPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPorts,
    CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumClusterwideNetworkPolicySpecEndpointSelector,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct

from cluster.cdk8s.providers.cilium.clusterwide_network_policy import ClusterwideNetworkPolicy
from cluster.cdk8s.providers.cilium.network_policy import (
    EgressRule,
    Entity,
    Protocol,
    dns_egress as _dns_egress,
    fqdn_fence as _fqdn_fence,
)
from cluster.cdk8s.service_ref import Pods

# kube-dns's own selector -- every environment's DNS egress rule selects it the same way.
KUBE_DNS_LABELS = {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}
# Authentik's server Pods, reached through the Gateway Service with the client's original SNI
# (cluster/docs/cilium_network_policy.md § Egress through a local Gateway).
AUTHENTIK_SERVER_LABELS = {
    "k8s:io.kubernetes.pod.namespace": "authentik",
    "app.kubernetes.io/name": "authentik",
    "app.kubernetes.io/instance": "authentik",
    "app.kubernetes.io/component": "server",
}
# Metrics scrapers: a server admits every Pod in `monitoring` on its metrics port.
SCRAPERS = Pods(namespace="monitoring", labels=())
# The uptime prober: Gatus requests every endpoint its config lists. Servers admit it through
# this and not through gatus/, so the prober's config can import the servers it probes.
PROBER = Pods(namespace="gatus", labels=(("app.kubernetes.io/name", "gatus"),))
# The agentplane-staging egress proxy: the caller of a service a sandbox reaches through it
# (agentplane/egress_staging_credentials.py). Servers admit it through this and not through
# agentplane/, so the proxy's config can import the servers it reaches.
AGENTPLANE_STAGING_PROXY = Pods(
    namespace="agentplane-staging", labels=(("app.kubernetes.io/name", "agentplane-egress"),)
)


def endpoint_labels(namespace: str, name: str) -> dict[str, str]:
    return {"k8s:io.kubernetes.pod.namespace": namespace, "app.kubernetes.io/name": name}


def egress_via_gateway(*server_names: str, port: int = 443) -> CiliumNetworkPolicySpecEgress:
    """A public origin the hostNetwork Gateway serves. It resolves to node IPs, which FQDN and
    CIDR selectors cannot match with the cluster's Cilium configuration; TLS SNI narrows the
    node:443 rule to that origin, and TLS stays end-to-end (no terminatingTLS secret, no MITM).

    **Never on an endpoint that also gets open egress on `port`.** `serverNames` is an L7 rule and
    Cilium enforces L7 per port on the endpoint, over the merge of every policy selecting it: one
    pinned rule makes the union of the `serverNames` lists in scope decide *all* node-IP HTTPS that
    endpoint makes, so any open `toEntities: [world, remote-node, host]:443` rule beside it stops
    being open for every name not listed here. `fleet_rules.https_egress_sni_conflicts` refuses the
    pair at synth. A workload that needs both -- agentplane's own egress proxy -- pins per backend
    port instead, or fences names in its own application-level allowlist.
    cluster/docs/cilium_network_policy.md section "An SNI rule decides port 443 for every policy
    that selects the same Pod"."""
    return EgressRule.to_entities(Entity.REMOTE_NODE, Entity.HOST, ports=[port], server_names=server_names)


def dns_egress(
    *, protocols: Sequence[Protocol] = ("UDP", "TCP"), resolves: Sequence[str] | None = None
) -> CiliumNetworkPolicySpecEgress:
    """kube-dns on port 53. `resolves` adds the DNS-aware rule that lets Cilium observe the
    answers a toFQDNs rule in the same policy needs, and bounds the query names to those
    matchers (`"*"` for any)."""
    return _dns_egress(KUBE_DNS_LABELS, protocols=protocols, resolves=resolves)


def open_internet_egress(
    *, ports: Sequence[int], entities: Sequence[Entity] = (Entity.WORLD, Entity.REMOTE_NODE, Entity.HOST)
) -> list[CiliumNetworkPolicySpecEgress]:
    """Fully open egress: unrestricted DNS resolution, then TCP `ports` to `entities`.

    `world` alone does not mean "everywhere": every `*.allegedly.works` name resolves to an OVH
    node ExternalIP, which Cilium carries as `reserved:remote-node`/`reserved:host`, not
    `reserved:world` -- a `world`-only rule black-holes traffic to this cluster's own public
    hostnames, so `remote_node`/`host` join the default. Widening a CIDR/FQDN rule cannot
    substitute: `policy-cidr-match-mode` is unset cluster-wide, so CIDR-derived selectors never
    match node IPs (cluster/docs/cilium_network_policy.md).
    """
    return [dns_egress(protocols=["ANY"], resolves=["*"]), EgressRule.to_entities(*entities, ports=ports)]


def fqdn_fence(
    *groups: Sequence[str], resolves_also: Sequence[str] = (), port: int = 443
) -> list[CiliumNetworkPolicySpecEgress]:
    """An FQDN allowlist's two halves from one host list: the DNS rule admitting exactly the
    names `groups` hold plus `resolves_also`, then one toFQDNs rule per group on TCP `port`.

    The DNS rule is not redundant with toFQDNs. The DNS proxy matches the query name before
    any destination identity exists, so it is the only layer that can fence a name resolving
    to a node IP, which toFQDNs cannot select (cluster/docs/cilium_network_policy.md); and
    Cilium has no way to share one list between the two rule kinds, so deriving both here is
    what keeps them equal. Both halves bound the selected Pod's own resolution and
    connections, not those of the workloads behind a proxy. SNI is not pinned: serverNames
    cannot carry the patterns a group may hold.
    """
    return _fqdn_fence(KUBE_DNS_LABELS, *groups, resolves_also=resolves_also, port=port)


def _to_ports(*ports: tuple[int, Protocol]) -> list[CiliumClusterwideNetworkPolicySpecEgressToPorts]:
    return [
        CiliumClusterwideNetworkPolicySpecEgressToPorts(
            ports=[
                CiliumClusterwideNetworkPolicySpecEgressToPortsPorts(
                    port=str(port), protocol=CiliumClusterwideNetworkPolicySpecEgressToPortsPortsProtocol[protocol]
                )
                for port, protocol in ports
            ]
        )
    ]


def force_proxy_egress(
    scope: Construct,
    id: str,
    *,
    name: str,
    namespaces: Sequence[str],
    proxy_namespace: str,
    proxy_name: str,
    proxy_port: int,
    cluster_ports: Sequence[int] | None,
    kube_apiserver: bool,
) -> ClusterwideNetworkPolicy:
    """Force the external egress of every Pod in `namespaces` through a proxy: admit kube-dns
    (plain L4), in-cluster traffic on TCP `cluster_ports` (any port when `None`), the
    kube-apiserver when `kube_apiserver`, and the Pods named `proxy_name` in `proxy_namespace` on
    TCP `proxy_port`. Cilium policies are additive, so any other egress policy selecting these
    Pods widens this one.
    """
    return ClusterwideNetworkPolicy(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name),
        endpoint_selector=CiliumClusterwideNetworkPolicySpecEndpointSelector(
            match_expressions=[
                CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressions(
                    key="k8s:io.kubernetes.pod.namespace",
                    operator=CiliumClusterwideNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                    values=list(namespaces),
                )
            ]
        ),
        egress=[
            CiliumClusterwideNetworkPolicySpecEgress(
                to_endpoints=[CiliumClusterwideNetworkPolicySpecEgressToEndpoints(match_labels=KUBE_DNS_LABELS)],
                to_ports=_to_ports((53, "UDP"), (53, "TCP")),
            ),
            CiliumClusterwideNetworkPolicySpecEgress(
                to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.CLUSTER],
                to_ports=_to_ports(*((port, "TCP") for port in cluster_ports)) if cluster_ports is not None else None,
            ),
            *(
                [
                    CiliumClusterwideNetworkPolicySpecEgress(
                        to_entities=[CiliumClusterwideNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER],
                        to_ports=_to_ports((6443, "TCP")),
                    )
                ]
                if kube_apiserver
                else []
            ),
            CiliumClusterwideNetworkPolicySpecEgress(
                to_endpoints=[
                    CiliumClusterwideNetworkPolicySpecEgressToEndpoints(
                        match_labels={
                            "k8s:io.kubernetes.pod.namespace": proxy_namespace,
                            "k8s:app.kubernetes.io/name": proxy_name,
                        }
                    )
                ],
                to_ports=_to_ports((proxy_port, "TCP")),
            ),
        ],
    )
