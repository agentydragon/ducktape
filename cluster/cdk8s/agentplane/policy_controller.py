"""The Policy Controller workload: it deletes expired PolicyBindings and keeps the native
RoleBindings and ClusterRoleBindings their Kubernetes rules expand to. Its RBAC is rendered by
policy_controller_rbac.py and binding_delegation.py.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "agentplane-policy-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-policy-controller"
_HEALTH_PORT = 8081
# How long a PolicyBinding may outlive its expiresAt before it alerts: the controller deletes it
# within seconds, so this is the controller being down or its deletes failing.
_OVERDUE_SECONDS = 300


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class PolicyController(Construct):
    """The controller's workload, service, network policy and alert; RBAC is a separate construct."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        namespace = env.namespace
        endpoint = metrics(namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="policy-controller",
            image=f"{_IMAGE}:unset",
            args=[f"--policy-namespace={namespace}", f"--subject-namespaces={namespace}"],
            ports=[endpoint.port.container_port(), Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
            ),
            termination_grace=Duration.seconds(30),
        )
        self.account = workload.account
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[cilium.SCRAPERS.admit(endpoint.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER)],
        )
        ServiceMonitor(
            self,
            "service-monitor",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            selector=ServiceMonitorSpecSelector(match_labels=endpoint.pods.selector),
            endpoints=[Endpoint.plain(port=endpoint.port.name)],
        )
        # kube-state-metrics reports every PolicyBinding's expiresAt (monitoring/stack.py), so this
        # fires even while the controller is down.
        PrometheusRule(
            self,
            "prometheus-rule",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            groups=[
                group(
                    NAME,
                    [
                        Rule.alert(
                            "AgentplanePolicyBindingExpiryOverdue",
                            f'time() - kube_customresource_agentplane_policybinding_expires_at{{namespace="{namespace}"}}'
                            f" > {_OVERDUE_SECONDS}",
                            labels={"severity": "warning"},
                            summary="PolicyBinding {{ $labels.name }} still grants after its expiresAt",
                            description=(
                                "The Policy Controller deletes a PolicyBinding at its expiresAt; this one has "
                                "outlived it, so its grants are still live. Check the agentplane-policy-controller "
                                "Deployment and its logs."
                            ),
                        )
                    ],
                )
            ],
        )
