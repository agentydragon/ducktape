"""The Policy Controller workload: it deletes expired PolicyBindings and keeps the native
RoleBindings and ClusterRoleBindings their Kubernetes rules expand to. Its RBAC is rendered by
policy_controller_rbac.py and binding_delegation.py.
"""

from __future__ import annotations

from collections.abc import Set

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector

from agentplane.policy_controller.settings import Settings
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.cert_manager.cluster_ca import WORKLOAD_ISSUER
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

NAME = "policy-controller"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-policy-controller"
_HEALTH_PORT = 8081
TOKEN_AUDIENCE = "agentplane-policy"
# How long a PolicyBinding may outlive its expiresAt before it alerts: the controller deletes it
# within seconds, so this is the controller being down or its deletes failing.
_OVERDUE_SECONDS = 300


_DEFAULT_MEMORY_REQUEST = Size.mebibytes(64)
_DEFAULT_MEMORY_LIMIT = Size.mebibytes(256)


def metrics(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="metrics", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8082), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class PolicyController(Construct):
    """The controller's workload and service; fleet policy and monitoring are added separately."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        accounts_namespace: str,
        workload_namespaces: Set[str],
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        namespace = env.namespace
        endpoint = metrics(namespace)
        settings = Settings(
            policy_namespace=namespace,
            subject_namespaces=frozenset({namespace}),
            accounts_namespace=accounts_namespace,
            metrics_bind_address=f":{endpoint.port.number}",
            health_probe_bind_address=f":{_HEALTH_PORT}",
            grpc_bind_address=f":{service(namespace).pod_port}",
            grpc_audience=TOKEN_AUDIENCE,
            workload_issuer=WORKLOAD_ISSUER.name,
            # policy_controller_rbac.py grants the issuer the same namespaces.
            workload_namespaces=workload_namespaces,
            reader_accounts=frozenset({f"{namespace}/app"}),
            writer_accounts=frozenset({f"{namespace}/app"}),
        )
        settings_file = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name="policy-controller-settings", namespace=namespace),
            content=yaml_config(settings),
            path="/etc/agentplane-policy-controller/config.yaml",
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint, service(namespace)],
            container="policy-controller",
            image=f"{_IMAGE}:unset",
            extra_ports=[Port(name="health", number=_HEALTH_PORT).container_port()],
            readiness=http_probe("/readyz", port=_HEALTH_PORT, initial_delay_seconds=3),
            liveness=http_probe("/healthz", port=_HEALTH_PORT, initial_delay_seconds=10, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        settings_file.mount_into(workload.container, env=Settings.config_file_env)
        self.account = workload.account
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-policy-controller-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )

    def add_platform_resources(self) -> None:
        """Add fleet network policy, scraping and expiry alert."""
        namespace = self.env.namespace
        endpoint = metrics(namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                cilium.SCRAPERS.admit(endpoint.pod_port),
                Pods(namespace=namespace, labels=()).admit(service(namespace).pod_port),
            ],
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
