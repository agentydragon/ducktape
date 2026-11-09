"""The authenticated byte-streaming ingress between Agentplane central egress and the
shared LiteLLM deployment.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    Volume,
)
from constructs import Construct

from agentplane.environment_runtime_transport.config import LLM_INGRESS_URI
from agentplane.llm_ingress.settings import Settings
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from agentplane.workload_auth.listener import TLS, KubernetesBearerAuth, Listener, SPIFFEAuth
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import central_sampler, workload_tls
from cluster.cdk8s.agentplane.codex_models import codex_catalog
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import tls_grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import checked_value, env_name, validate_settings_fragment

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "llm-ingress"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/agentplane-llm-ingress"
_LABELS = {"app.kubernetes.io/name": _NAME}
# The Codex catalog is its own ConfigMap, so a catalog edit does not change the settings file.
_CODEX_CATALOG_PATH = PurePosixPath("/etc/agentplane-llm-ingress-codex/codex_models.json")


_DEFAULT_MEMORY_REQUEST = Size.mebibytes(128)
_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)


def service(namespace: str) -> ServiceRef:
    """The LLM ingress in one environment's namespace."""
    return ServiceRef(
        name=_NAME, port=Port(name="rpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def proxied_url(namespace: str) -> str:
    """The LLM ingress as a workload dials it through the egress proxy, which matches the host on the
    exact string its policy names."""
    llm = service(namespace)
    return f"http://{llm.fqdn}:{llm.port.number}"


class LlmIngress(Construct):
    """The LLM ingress's service workload, cluster TokenReview RBAC and settings ConfigMap.
    Fleet Cilium policy is added separately.
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
        memory_request: Size = _DEFAULT_MEMORY_REQUEST,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.service = service(env.namespace)
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
        # The ingress calls TokenReview as itself, so it keeps the helper's mounted token.
        workload = add_service_workload(
            self,
            env,
            serves=[self.service],
            container="ingress",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            annotations={
                "configmap.reloader.stakater.com/reload": ",".join(
                    config_map
                    for config_map in (f"{_NAME}-settings", None if catalog_map is None else catalog_map.name)
                    if config_map is not None
                ),
                "secret.reloader.stakater.com/reload": env.llm_ingress.litellm_key_secret_name,
            },
            env_variables={
                **{
                    env_name(Settings, field): EnvValue.from_value(str(checked_value(Settings, field, value)))
                    for field, value in {"token_audience": WORKLOAD_TOKEN_AUDIENCE, "litellm_url": litellm_url}.items()
                },
                # The only real model credential in this service; runners never mount it.
                env_name(Settings, "litellm_key"): SecretRef(
                    namespace=env.namespace, name=env.llm_ingress.litellm_key_secret_name
                )
                .key("api-key")
                .env_value(self, "litellm-key-secret"),
            },
            readiness=tls_grpc_probe(
                port=self.service.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=LLM_INGRESS_URI,
                initial_delay_seconds=3,
                period_seconds=10,
            ),
            liveness=tls_grpc_probe(
                port=self.service.pod_port,
                ca_file=workload_tls.TRUST_BUNDLE_FILE,
                server_uri=LLM_INGRESS_URI,
                initial_delay_seconds=20,
                period_seconds=30,
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=memory_request, limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            tolerate_control_plane=True,
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        # Backend callers retain their Pod-bound TokenReview credentials. The
        # proxy tunnels Sandbox TLS and asserts no caller identity.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-llm-ingress-token-reviewer",
            service_account_name=workload.account.name,
            namespace=env.namespace,
        )
        reader_role_name = f"{_NAME}-sandbox-reader"
        reader_role = Role(
            self,
            "sandbox-reader-role",
            metadata=ApiObjectMetadata(name=reader_role_name, namespace=env.namespace),
            rules=[
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "sandboxes")], verbs=["get", "list"]
                ),
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
                # A caller's SVID names its WorkloadCredential, read live on every request.
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "workloadcredentials")], verbs=["get"]
                ),
            ],
        )
        RoleBinding(
            self,
            "sandbox-reader-rolebinding",
            metadata=ApiObjectMetadata(name=reader_role_name, namespace=env.namespace),
            role=reader_role,
        ).add_subjects(workload.account)
        if catalog_map is not None:
            workload.container.mount(
                str(_CODEX_CATALOG_PATH.parent),
                Volume.from_config_map(self, "codex-models-volume", catalog_map),
                read_only=True,
            )
        # Settings this deployment supplies as YAML rather than flags, so a list is a list.
        content = {
            "allowed_service_account_namespaces": [env.namespace],
            **(
                {"central_sampler_accounts": [f"{env.namespace}/{central_sampler.NAME}"]}
                if env.central_sampler is not None
                else {}
            ),
            "log_llm_requests": env.llm_ingress.log_llm_requests,
            "models": [item.model_dump(mode="json") for item in env.llm_ingress.models],
            "attribution": env.llm_ingress.attribution.model_dump(mode="json"),
            **({} if catalog_map is None else {"codex_model_catalog_file": str(_CODEX_CATALOG_PATH)}),
        }
        serving_tls = workload_tls.serving_certificate(
            self, env, container=workload.container, name=_NAME, uri=LLM_INGRESS_URI
        )
        assert serving_tls.cert_file is not None
        assert serving_tls.key_file is not None
        assert serving_tls.ca_file is not None
        content["rpc"] = Listener(
            listen_address=f"0.0.0.0:{self.service.pod_port}",
            tls=TLS(cert_file=Path(serving_tls.cert_file), key_file=Path(serving_tls.key_file)),
            auth=[
                SPIFFEAuth(ca_file=Path(serving_tls.ca_file)),
                KubernetesBearerAuth(audiences=frozenset({WORKLOAD_TOKEN_AUDIENCE})),
            ],
        ).model_dump(mode="json")
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{_NAME}-settings", namespace=env.namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            path="/etc/agentplane-llm-ingress/settings.yaml",
        )
        settings.mount_into(workload.container, env=Settings.config_file_env)

    def add_platform_resources(self) -> None:
        """Add fleet Cilium network admission."""
        self._add_network_policy()

    def _add_network_policy(self) -> None:
        # Existing proxy and sampler callers share the verified TLS listener.
        # The ingress reaches DNS, the API server, and LiteLLM.
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=_NAME.removeprefix("agentplane-"), namespace=self.env.namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                # egress.py imports this module, so the proxy's Pods are named here.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(self.env.namespace, "egress"), ports=[self.service.pod_port]
                ),
                *(
                    [central_sampler.pods(self.env.namespace).admit(self.service.pod_port)]
                    if self.env.central_sampler is not None
                    else []
                ),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": "litellm", "k8s:app.kubernetes.io/name": "litellm"}, 4000
                ),
            ],
        )
