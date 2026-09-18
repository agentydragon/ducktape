"""Reusable cdk8s constructs for the Agentplane staging/testing environments'
llm-ingress/ directory: the authenticated byte-streaming ingress between Agentplane
central egress and the shared LiteLLM deployment.

The Deployment's image tag is a deliberate placeholder ("unset") -- the sibling
image-pins/ Kustomize Component (hand-written, never generated) overrides it at
`kustomize build` time via Flux's image-automation marker. See cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    Capability,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
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
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToEntities,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct

from cluster.cdk8s.agentplane import cilium_helpers, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-llm-ingress"
_IMAGE_NAME = "git.allegedly.works/ducktape-ci/agentplane-llm-ingress"
CONTAINER_PORT = 8080
_LABELS = {"app.kubernetes.io/name": _NAME}
_SETTINGS_PATH = "/etc/agentplane-llm-ingress/settings.yaml"


@dataclass(frozen=True)
class LlmIngressEnvSpec:
    """Per-environment values for the LLM ingress Deployment."""

    namespace: str
    replicas: int
    strategy: DeploymentStrategy
    # staging spreads its 2 replicas across nodes; testing's single replica has
    # nothing to spread.
    topology_spread: bool
    litellm_key_secret_name: str


class LlmIngress(Construct):
    """ServiceAccount, cluster TokenReview RBAC, the settings ConfigMap, Deployment,
    Service, and CiliumNetworkPolicy for the LLM ingress.
    """

    def __init__(self, scope: Construct, id: str, spec: LlmIngressEnvSpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the ingress
        # calls TokenReview as itself, so it needs its own mounted token -- opt back
        # in explicitly to preserve today's actual (and required) behavior.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(_NAME, spec.namespace), automount_token=True
        )
        # TokenReview proves the Pod-bound workload bearer presented by the central
        # egress proxy. It grants none of that Pod's authority to the ingress.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{spec.namespace}-llm-ingress-token-reviewer",
            service_account_name=_NAME,
            namespace=spec.namespace,
        )
        settings_cm = self._add_settings_configmap()
        deployment = self._add_deployment(service_account, settings_cm)
        self._add_service(deployment)
        self._add_network_policy()

    def _add_settings_configmap(self) -> ConfigMap:
        return ConfigMap(
            self,
            "settings",
            metadata=metadata(f"{_NAME}-settings", self.spec.namespace),
            data={"settings.yaml": yaml_config({"allowed_service_account_namespaces": [self.spec.namespace]})},
        )

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                self.spec.namespace,
                labels=_LABELS,
                annotations={"secret.reloader.stakater.com/reload": self.spec.litellm_key_secret_name},
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.spec.replicas,
            strategy=self.spec.strategy,
            service_account=service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token (Kubernetes uses whichever is explicitly set at the
            # narrower pod scope) -- opt in for the same reason as the ServiceAccount above.
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
        )
        deployment.add_container(
            name="ingress",
            image=f"{_IMAGE_NAME}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=[
                "--token-audience=agentplane-egress",
                "--litellm-url=http://litellm.litellm.svc.cluster.local:4000",
                "--host=0.0.0.0",
                f"--port={CONTAINER_PORT}",
            ],
            env_variables={
                # The only real model credential in this service; runners never mount it.
                "AGENTPLANE_LLM_INGRESS_LITELLM_KEY": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "litellm-key-secret", self.spec.litellm_key_secret_name),
                        key="api-key",
                    )
                ),
                # Settings this deployment supplies as YAML rather than flags, so a list is a list.
                "AGENTPLANE_LLM_INGRESS_CONFIG_FILE": EnvValue.from_value(_SETTINGS_PATH),
            },
            ports=[ContainerPort(name="http", number=CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/healthz", port=CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            # Same rationale as litellm_constructs.py's container securityContext
            # override: cdk8s_plus_34 defaults to a hardened readOnlyRootFilesystem,
            # but this container's actual root needs haven't been audited, so
            # silently hardening it here could break the running ingress.
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                read_only_root_filesystem=False,
            ),
        )

        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount(_SETTINGS_PATH, settings_volume, sub_path="settings.yaml", read_only=True)

        node_scheduling.attract_to_zone(deployment)
        node_scheduling.tolerate_control_plane_taint(deployment)
        apply_pod_spec_patches(deployment, labels=_LABELS, topology_spread=self.spec.topology_spread)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.spec.namespace),
            selector=deployment,
            ports=[ServicePort(name="http", port=CONTAINER_PORT, target_port=CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_network_policy(self) -> None:
        # Only central egress can call the workload-authenticated listener. The
        # ingress can reach only DNS, TokenReview at the API server, and the
        # existing LiteLLM Service.
        CiliumNetworkPolicy(
            self,
            "networkpolicy",
            metadata=metadata(_NAME, self.spec.namespace),
            spec=CiliumNetworkPolicySpec(
                endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_LABELS),
                ingress=[
                    CiliumNetworkPolicySpecIngress(
                        from_endpoints=[
                            CiliumNetworkPolicySpecIngressFromEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": self.spec.namespace,
                                    "app.kubernetes.io/name": "agentplane-egress",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port=str(CONTAINER_PORT),
                                        protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    )
                ],
                egress=[
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(match_labels=cilium_helpers.KUBE_DNS_LABELS)
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.UDP
                                    ),
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    ),
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_entities=[CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER]
                    ),
                    cilium_helpers.tcp_egress_to(
                        {"k8s:io.kubernetes.pod.namespace": "litellm", "k8s:app.kubernetes.io/name": "litellm"}, 4000
                    ),
                ],
            ),
        )
