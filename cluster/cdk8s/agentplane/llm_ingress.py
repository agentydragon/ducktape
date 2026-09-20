"""The authenticated byte-streaming ingress between Agentplane central egress and the
shared LiteLLM deployment.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from constructs import Construct

from agentplane.llm_ingress.main import CONFIG_FILE_ENV, Settings
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import container_security, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name, settings_file

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-llm-ingress"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/agentplane-llm-ingress"
CONTAINER_PORT = 8080
_LABELS = {"app.kubernetes.io/name": _NAME}
_SETTINGS_PATH = "/etc/agentplane-llm-ingress/settings.yaml"
# The Sandbox runner's workload token audience: minted once by the runner
# (app.py's projected ServiceAccountToken), verified unchanged by the
# central egress proxy, then forwarded and verified again here -- every hop must accept
# the same audience string.
WORKLOAD_TOKEN_AUDIENCE = "agentplane-egress"


class LlmIngress(Construct):
    """ServiceAccount, cluster TokenReview RBAC, the settings ConfigMap, Deployment,
    Service, and CiliumNetworkPolicy for the LLM ingress.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the ingress
        # calls TokenReview as itself, so it needs its own mounted token.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(_NAME, env.namespace), automount_token=True
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
        settings_cm = self._add_settings_configmap()
        deployment = self._add_deployment(service_account, settings_cm)
        self._add_service(deployment)
        self._add_network_policy()

    def _add_settings_configmap(self) -> ConfigMap:
        return ConfigMap(
            self,
            "settings",
            metadata=metadata(f"{_NAME}-settings", self.env.namespace),
            data={
                "settings.yaml": yaml_config(
                    settings_file(Settings, {"allowed_service_account_namespaces": [self.env.namespace]})
                )
            },
        )

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                self.env.namespace,
                labels=_LABELS,
                annotations={"secret.reloader.stakater.com/reload": self.env.llm_ingress.litellm_key_secret_name},
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
                port=CONTAINER_PORT,
            ),
            env_variables={
                # The only real model credential in this service; runners never mount it.
                env_name(Settings, "litellm_key"): EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(
                            self, "litellm-key-secret", self.env.llm_ingress.litellm_key_secret_name
                        ),
                        key="api-key",
                    )
                ),
                # Settings this deployment supplies as YAML rather than flags, so a list is a list.
                CONFIG_FILE_ENV: EnvValue.from_value(_SETTINGS_PATH),
            },
            ports=[ContainerPort(name="http", number=CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/healthz", port=CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )

        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount(_SETTINGS_PATH, settings_volume, sub_path="settings.yaml", read_only=True)

        node_scheduling.attract_to_zone(deployment)
        node_scheduling.tolerate_control_plane_taint(deployment)
        apply_pod_spec_patches(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.env.namespace),
            selector=deployment,
            ports=[ServicePort(name="http", port=CONTAINER_PORT, target_port=CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_network_policy(self) -> None:
        # Only central egress can call the workload-authenticated listener. The
        # ingress can reach only DNS, TokenReview at the API server, and the
        # existing LiteLLM Service.
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(_NAME, self.env.namespace),
            selector=_LABELS,
            ingress=[
                cilium.ingress_from(
                    cilium.endpoint_labels(self.env.namespace, "agentplane-egress"), ports=[CONTAINER_PORT]
                )
            ],
            egress=[
                cilium.dns_egress(),
                cilium.egress_to_entities("kube-apiserver"),
                cilium.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": "litellm", "k8s:app.kubernetes.io/name": "litellm"}, 4000
                ),
            ],
        )
