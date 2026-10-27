"""The authenticated byte-streaming ingress between Agentplane central egress and the
shared LiteLLM deployment.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import protovalidate
from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Secret,
    Service,
    ServiceAccount,
    Volume,
)
from constructs import Construct

from agentplane.llm_ingress.configpb import config_pb2
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import telemetry
from cluster.cdk8s.agentplane.codex_models import codex_catalog
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.protobuf import message_dict

# Generated message stubs import protobuf runtime types.
# gazelle:include_dep @pypi//protobuf

_CONFIG_FILE_ENV = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
_ENV_PREFIX = "AGENTPLANE_LLM_INGRESS_"

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "llm-ingress"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/agentplane-llm-ingress"
_LABELS = {"app.kubernetes.io/name": _NAME}
# The Codex catalog is its own ConfigMap, so a catalog edit does not change the settings file.
_CODEX_CATALOG_PATH = PurePosixPath("/etc/agentplane-llm-ingress-codex/codex_models.json")


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)


def service(namespace: str) -> ServiceRef:
    """The LLM ingress in one environment's namespace."""
    return ServiceRef(
        name=_NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def proxied_url(namespace: str) -> str:
    """The LLM ingress as a workload dials it through the egress proxy, which matches the host on the
    exact string its policy names."""
    llm = service(namespace)
    return f"http://{llm.fqdn}:{llm.port.number}"


class LlmIngress(Construct):
    """ServiceAccount, cluster TokenReview RBAC, the settings ConfigMap, Deployment,
    and Service for the LLM ingress. Fleet Cilium policy is added separately.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        litellm_url: str = "http://litellm.litellm.svc.cluster.local:4000",
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.litellm_url = litellm_url
        self.placement = placement
        self.registry_secret_name = registry_secret_name
        self.memory_limit = memory_limit
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
        catalog = codex_catalog(env.llm_ingress.codex_routes)
        catalog_map = (
            None
            if catalog is None
            else ConfigMap(
                self,
                "codex-models",
                metadata=ApiObjectMetadata(name=f"{_NAME}-codex-models", namespace=env.namespace),
                data={_CODEX_CATALOG_PATH.name: catalog},
            )
        )
        # This file is a source fragment. Startup validates it together with the
        # environment, including the actual Secret; its nested file values are complete.
        content = config_pb2.Config(
            allowed_service_account_namespaces=[env.namespace],
            log_llm_requests=env.llm_ingress.log_llm_requests,
            models=env.llm_ingress.models,
            attribution=env.llm_ingress.attribution,
            codex_model_catalog_file=str(_CODEX_CATALOG_PATH) if catalog_map is not None else "",
        )
        for model in content.models:
            protovalidate.validate(model)
        protovalidate.validate(content.attribution)
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{_NAME}-settings", namespace=env.namespace),
            content=yaml_config(message_dict(content)),
            path="/etc/agentplane-llm-ingress/settings.yaml",
        )
        deployment = self._add_deployment(service_account, settings, catalog_map)
        self._add_service(deployment)

    def add_platform_resources(self) -> None:
        """Add fleet Cilium network admission."""
        self._add_network_policy()

    def _add_deployment(
        self, service_account: ServiceAccount, settings: SettingsFile, catalog_map: ConfigMap | None
    ) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=self.env.namespace,
                labels=_LABELS,
                annotations={
                    "configmap.reloader.stakater.com/reload": ",".join(
                        config_map.name for config_map in (settings.config_map, catalog_map) if config_map is not None
                    ),
                    "secret.reloader.stakater.com/reload": self.env.llm_ingress.litellm_key_secret_name,
                },
            ),
            pod_metadata=ApiObjectMetadata(labels={**_LABELS, **telemetry.EMITTER_SERVICE_CLASS_LABELS}),
            replicas=self.env.replicas.count,
            strategy=self.env.replicas.strategy,
            service_account=service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token -- opt in for the same reason as the ServiceAccount.
            automount_service_account_token=True,
            docker_registry_auth=(
                Secret.from_secret_name(self, "forgejo-images-creds-ref", self.registry_secret_name)
                if self.registry_secret_name is not None
                else None
            ),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.add_container(
            name="ingress",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            env_variables={
                f"{_ENV_PREFIX}TOKEN_AUDIENCE": EnvValue.from_value(WORKLOAD_TOKEN_AUDIENCE),
                f"{_ENV_PREFIX}LITELLM_URL": EnvValue.from_value(self.litellm_url),
                f"{_ENV_PREFIX}HOST": EnvValue.from_value("0.0.0.0"),
                f"{_ENV_PREFIX}PORT": EnvValue.from_value(str(self.service.pod_port)),
                **{
                    name: EnvValue.from_value(value)
                    for name, value in telemetry.service_environment(self.env.namespace, _NAME).items()
                },
                # The only real model credential in this service; runners never mount it.
                f"{_ENV_PREFIX}LITELLM_KEY": SecretRef(
                    namespace=self.env.namespace, name=self.env.llm_ingress.litellm_key_secret_name
                )
                .key("api-key")
                .env_value(self, "litellm-key-secret"),
            },
            ports=[self.service.port.container_port()],
            readiness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=self.memory_limit),
            ),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )

        settings.mount_into(deployment.containers[0], env=_CONFIG_FILE_ENV)
        if catalog_map is not None:
            deployment.containers[0].mount(
                str(_CODEX_CATALOG_PATH.parent),
                Volume.from_config_map(self, "codex-models-volume", catalog_map),
                read_only=True,
            )

        if self.placement is not None:
            pod_policy.place(deployment, self.placement, tolerate_control_plane=True)
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
            metadata=ApiObjectMetadata(name=_NAME.removeprefix("agentplane-"), namespace=self.env.namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                # egress.py imports this module, so the proxy's Pods are named here.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(self.env.namespace, "egress"), ports=[self.service.pod_port]
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
