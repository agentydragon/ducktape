"""The authenticated byte-streaming ingress between Agentplane central egress and the
shared LiteLLM deployment.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    ServiceAccount,
)
from constructs import Construct

from agentplane.llm_ingress.models import ModelConfig
from agentplane.llm_ingress.settings import CONFIG_FILE_ENV, Settings
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.model_selections import RUNNER_CONTEXT_OVERRIDES, HarnessRoutes
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-llm-ingress"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/agentplane-llm-ingress"
_LABELS = {"app.kubernetes.io/name": _NAME}
# The Sandbox runner's workload token audience: minted once by the runner
# (app.py's projected ServiceAccountToken), verified unchanged by the
# central egress proxy, then forwarded and verified again here -- every hop must accept
# the same audience string.
WORKLOAD_TOKEN_AUDIENCE = "agentplane-egress"


def model_configs(models: HarnessRoutes) -> list[ModelConfig]:
    return [
        ModelConfig(model=route.id, total_context_budget_tokens=RUNNER_CONTEXT_OVERRIDES[route])
        for route in sorted(models.all, key=lambda route: route.id)
        if route in RUNNER_CONTEXT_OVERRIDES
    ]


def service(namespace: str) -> ServiceRef:
    """The LLM ingress in one environment's namespace."""
    return ServiceRef(
        name=_NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


class LlmIngress(Construct):
    """ServiceAccount, cluster TokenReview RBAC, the settings ConfigMap, Deployment,
    Service, and CiliumNetworkPolicy for the LLM ingress.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env
        self.service = service(env.namespace)

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the ingress
        # calls TokenReview as itself, so it needs its own mounted token.
        service_account = ServiceAccount(
            self,
            "serviceaccount",
            metadata=ApiObjectMetadata(name=_NAME, namespace=env.namespace),
            automount_token=True,
        )
        # TokenReview proves the Pod-bound workload bearer presented by the central
        # egress proxy. It grants none of that Pod's authority to the ingress.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-llm-ingress-token-reviewer",
            service_account_name=_NAME,
            namespace=env.namespace,
        )
        # Settings this deployment supplies as YAML rather than flags, so a list is a list.
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{_NAME}-settings", namespace=env.namespace),
            model=Settings,
            content={
                "allowed_service_account_namespaces": [env.namespace],
                "log_llm_requests": env.llm_ingress.log_llm_requests,
                "models": [item.model_dump(mode="json") for item in env.llm_ingress.models],
            },
            path="/etc/agentplane-llm-ingress/settings.yaml",
        )
        deployment = self._add_deployment(service_account, settings)
        self._add_service(deployment)
        self._add_network_policy()

    def _add_deployment(self, service_account: ServiceAccount, settings: SettingsFile) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=self.env.namespace,
                labels=_LABELS,
                annotations={
                    "configmap.reloader.stakater.com/reload": settings.config_map.name,
                    "secret.reloader.stakater.com/reload": self.env.llm_ingress.litellm_key_secret_name,
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.env.replicas.count,
            strategy=self.env.replicas.strategy,
            service_account=service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token -- opt in for the same reason as the ServiceAccount.
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.add_container(
            name="ingress",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=cli_args(
                Settings,
                token_audience=WORKLOAD_TOKEN_AUDIENCE,
                litellm_url="http://litellm.litellm.svc.cluster.local:4000",
                host="0.0.0.0",
                port=self.service.pod_port,
            ),
            env_variables={
                # The only real model credential in this service; runners never mount it.
                env_name(Settings, "litellm_key"): SecretRef(
                    namespace=self.env.namespace, name=self.env.llm_ingress.litellm_key_secret_name
                )
                .key("api-key")
                .env_value(self, "litellm-key-secret")
            },
            ports=[self.service.port.container_port()],
            readiness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )

        settings.mount_into(deployment.containers[0], env=CONFIG_FILE_ENV)

        pod_policy.place(deployment, node_scheduling.HIL_OVH, tolerate_control_plane=True)
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=self.service.name, namespace=self.env.namespace),
            selector=deployment,
            ports=[self.service.port.service_port()],
        )

    def _add_network_policy(self) -> None:
        # Only central egress can call the workload-authenticated listener. The
        # ingress can reach only DNS, TokenReview at the API server, and the
        # existing LiteLLM Service.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=_NAME, namespace=self.env.namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                # egress.py imports this module, so the proxy's Pods are named here.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(self.env.namespace, "agentplane-egress"), ports=[self.service.pod_port]
                )
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": "litellm", "k8s:app.kubernetes.io/name": "litellm"}, 4000
                ),
            ],
        )
