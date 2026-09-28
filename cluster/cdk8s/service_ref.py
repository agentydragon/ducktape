"""Identity-only references to a Service, its ports and the Pods behind it, declared once by the
module that builds them and projected into each dialect by methods: container and Service ports,
the client's address, and the Cilium rules on either side of a connection.

A reference holds names and the addresses derived from them, never spec.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s_plus_34 import ContainerPort, Protocol, ServicePort, k8s
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress, CiliumNetworkPolicySpecIngress

from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule


@dataclass(frozen=True)
class Port:
    """A named TCP port. Its Service port targets the container port of the same number; a Service
    whose Pods listen elsewhere states that on its `ServiceRef.target_port`."""

    name: str
    number: int

    def container_port(self) -> ContainerPort:
        return ContainerPort(name=self.name, number=self.number, protocol=Protocol.TCP)

    def service_port(self) -> ServicePort:
        return ServicePort(name=self.name, port=self.number, target_port=self.number, protocol=Protocol.TCP)

    def k8s_container_port(self) -> k8s.ContainerPort:
        return k8s.ContainerPort(name=self.name, container_port=self.number, protocol="TCP")

    def k8s_service_port(self) -> k8s.ServicePort:
        return k8s.ServicePort(
            name=self.name, port=self.number, target_port=k8s.IntOrString.from_number(self.number), protocol="TCP"
        )


@dataclass(frozen=True)
class Pods:
    """Pods by namespace and labels. No labels means every Pod in the namespace."""

    namespace: str
    labels: tuple[tuple[str, str], ...]

    @property
    def selector(self) -> dict[str, str]:
        return dict(self.labels)

    @property
    def cilium(self) -> dict[str, str]:
        """The endpoint labels a Cilium rule matches these Pods by."""
        return {"k8s:io.kubernetes.pod.namespace": self.namespace, **self.selector}

    def admit(self, *ports: int) -> CiliumNetworkPolicySpecIngress:
        """The server-side rule letting these Pods in; the server passes its own `pod_port`."""
        return IngressRule.from_endpoints(self.cilium, ports=ports)


@dataclass(frozen=True)
class _Service:
    name: str
    port: Port  # the Service port clients dial: `url` and an HTTPRoute backendRef
    pods: Pods

    @property
    def labels(self) -> dict[str, str]:
        """The Service's own labels; a ServiceMonitor selects on these."""
        return self.pods.selector

    @property
    def host(self) -> str:
        return f"{self.name}.{self.pods.namespace}.svc"

    @property
    def fqdn(self) -> str:
        """For a host compared as a string, and for a client in a hostNetwork Pod, which does not
        get the cluster search path."""
        return f"{self.host}.cluster.local"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port.number}"


@dataclass(frozen=True)
class ServiceRef(_Service):
    """A Service and the Pods behind it. A Service with several ports is several ServiceRefs
    sharing `name` and `pods`."""

    target_port: int | None = None  # the Pods' port where it differs from `port` (a Helm chart's 80 -> 8080)

    @property
    def pod_port(self) -> int:
        return self.port.number if self.target_port is None else self.target_port

    def egress(self) -> CiliumNetworkPolicySpecEgress:
        """The client's rule. Cilium matches after socket-LB translation, so it names the Pods' port."""
        return EgressRule.to_endpoints(self.pods.cilium, self.pod_port)


@dataclass(frozen=True)
class HostNetworkServiceRef(_Service):
    """A Service in front of hostNetwork Pods. Cilium identifies their traffic by node, not by
    endpoint, so there is no `egress()`: a client admits the node entities on `port`."""
