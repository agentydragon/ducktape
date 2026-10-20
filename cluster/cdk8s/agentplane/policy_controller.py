"""The Policy Controller workload: it deletes expired PolicyBindings and keeps the native
RoleBindings and ClusterRoleBindings their Kubernetes rules expand to. Its RBAC is rendered by
policy_controller_rbac.py and binding_delegation.py.
"""

from __future__ import annotations

from collections.abc import Mapping

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, EnvValue, MemoryResources
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.policy_controller.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.settings_contract import checked_value, env_name

NAME = "policy-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-policy-controller"
_HEALTH_PORT = 8081
# How long a PolicyBinding may outlive its expiresAt before it alerts: the controller deletes it
# within seconds, so this is the controller being down or its deletes failing.
_OVERDUE_SECONDS = 300


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(256)


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class PolicyController(Construct):
    """The controller's workload and service; fleet policy and monitoring are added separately."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        service_cluster_ip: str | None = None,
        container_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        namespace = env.namespace
        endpoint = metrics(namespace)
        scalars = {
            field: checked_value(Settings, field, value)
            for field, value in {
                "policy_namespace": namespace,
                "metrics_bind_address": f":{endpoint.port.number}",
                "health_probe_bind_address": f":{_HEALTH_PORT}",
            }.items()
        }
        subject_namespaces = checked_value(Settings, "subject_namespaces", frozenset({namespace}))
        text = {**scalars, "subject_namespaces": ",".join(sorted(subject_namespaces))}
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="policy-controller",
            image=f"{_IMAGE}:unset",
            env_variables={
                **{env_name(Settings, field): EnvValue.from_value(value) for field, value in text.items()},
                **(container_env or {}),
            },
            extra_ports=[Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(64), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            placement=placement,
            registry_secret_name=registry_secret_name,
            service_cluster_ip=service_cluster_ip,
        )
        self.account = workload.account

    def add_platform_resources(self) -> None:
        """Add fleet network policy, scraping and expiry alert."""
        namespace = self.env.namespace
        endpoint = metrics(namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
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
