"""Reusable cdk8s constructs for the Agentplane staging/testing environments'
egress/ directory: the central egress proxy, its interception CA/trust bundle, and
the EgressCredential/EgressPolicy resources it reads.

The Deployment's image tags are deliberate placeholders ("unset") -- the sibling
image-pins/ Kustomize Component (hand-written, never generated) overrides them at
`kustomize build` time via Flux's image-automation marker. See cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecSource,
    EgressCredentialSpecSourceSecretRef,
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicy,
    EgressPolicySpec,
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ApiResource,
    Capability,
    ConfigMap,
    ContainerPort,
    ContainerProps,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    Node,
    NodeLabelQuery,
    NodeTaintQuery,
    PodSecurityContextProps,
    Protocol,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    TaintEffect,
    Volume,
    k8s,
)
from cert_manager_crds.io.cert_manager import (
    Certificate,
    CertificateSpec,
    CertificateSpecIssuerRef,
    CertificateSpecPrivateKey,
    CertificateSpecPrivateKeyAlgorithm,
    CertificateSpecSecretTemplate,
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
    CiliumNetworkPolicySpecEgressToPortsRules,
    CiliumNetworkPolicySpecEgressToPortsRulesDns,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesSecret,
    BundleSpecTarget,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-egress"
_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-migrate"
_ZONE = "hil-ovh"
_LABELS = {"app.kubernetes.io/name": _NAME}
_PROXY_PORT = 8888
_ADMIN_PORT = 8081
_AGENT_API_PORT = 8082
_ROOT_CA_ISSUER = "cluster-ca-bootstrap"


# cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing
# IApiResource's `resource_name` member (see namespace_rbac_constructs.py's `_custom`,
# same cast for the same reason).
def _custom(api_group: str, resource_type: str) -> IApiResource:
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))


@dataclass(frozen=True)
class EgressEnvSpec:
    """Per-environment values for the egress proxy."""

    namespace: str
    # The interception CA's Secret/Bundle/ConfigMap name -- asymmetric between
    # environments today (unprefixed for staging, namespace-prefixed for testing).
    # Threaded through explicitly rather than derived, to preserve that as-is.
    ca_secret_name: str
    replicas: int
    strategy: DeploymentStrategy
    min_ready_seconds: int | None
    # staging spreads its 2 replicas across nodes; testing's single replica has
    # nothing to spread.
    topology_spread: bool
    enable_pdb: bool


def _egress_credentials(scope: Construct, *, namespace: str) -> None:
    EgressCredential(
        scope,
        "egresscredential-agentplane-workload",
        metadata=ApiObjectMetadata(name="agentplane-workload", namespace=namespace),
        spec=EgressCredentialSpec(
            description=(
                "The calling Sandbox Pod's short-lived, Pod-bound workload identity for first-party "
                "Agentplane destinations. It conveys no LiteLLM credential or operator, Agent, or "
                "Thread authority."
            ),
            source=EgressCredentialSpecSource(authenticated_workload_token={}),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        ),
    )
    EgressCredential(
        scope,
        "egresscredential-github-pat",
        metadata=ApiObjectMetadata(name="github-pat", namespace=namespace),
        spec=EgressCredentialSpec(
            description=(
                "A GitHub personal access token belonging to the bot account agentydragon-agent. "
                "Requests carrying it act as that account and are attributable to it. The proxy does "
                "not narrow what the token itself may do — the rule's hosts and methods are the only "
                "limit it adds, so treat anything the token can reach on those hosts as reachable."
            ),
            source=EgressCredentialSpecSource(
                secret_ref=EgressCredentialSpecSourceSecretRef(name="agentplane-github-pat", key="token")
            ),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                ),
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD
                ),
            ],
        ),
    )


def _egress_policies(scope: Construct, *, namespace: str) -> None:
    EgressPolicy(
        scope,
        "egresspolicy-basic",
        metadata=ApiObjectMetadata(name="basic", namespace=namespace),
        spec=EgressPolicySpec(
            rules=[
                EgressPolicySpecRules(
                    hosts=[f"agentplane-llm-ingress.{namespace}.svc.cluster.local"],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
                ),
                EgressPolicySpecRules(
                    hosts=[f"agentplane-actions.{namespace}.svc.cluster.local"],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                    paths=[
                        "/mcp",
                        "/openapi.json",
                        "/v1/action-groups",
                        "/v1/action-groups/**",
                        "/v1/action-policy",
                        "/v1/action-requests",
                        "/v1/action-requests/**",
                    ],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
                ),
                EgressPolicySpecRules(
                    hosts=[f"agentplane-egress.{namespace}.svc.cluster.local"],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.GET],
                    paths=["/openapi.json", "/v1/rules"],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
                ),
            ]
        ),
    )
    EgressPolicy(
        scope,
        "egresspolicy-github-public",
        metadata=ApiObjectMetadata(name="github-public", namespace=namespace),
        spec=EgressPolicySpec(
            rules=[
                EgressPolicySpecRules(
                    hosts=["api.github.com", "github.com", "*.githubusercontent.com"],
                    methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name="github-pat"),
                )
            ]
        ),
    )


class Egress(Construct):
    """The central egress proxy: ServiceAccount, RBAC, interception CA/trust bundle,
    Deployment, Services, CiliumNetworkPolicy, optional PodDisruptionBudget, and the
    EgressCredential/EgressPolicy resources it reads.
    """

    def __init__(self, scope: Construct, id: str, spec: EgressEnvSpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the proxy
        # calls TokenReview as itself, so it needs its own mounted token.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(_NAME, spec.namespace), automount_token=True
        )
        self._add_rbac(service_account)
        self._add_certificate_and_bundle()
        settings_cm = self._add_settings_configmap()
        deployment = self._add_deployment(service_account, settings_cm)
        self._add_services(deployment)
        self._add_network_policy()
        if spec.enable_pdb:
            self._add_pdb()
        _egress_credentials(self, namespace=spec.namespace)
        _egress_policies(self, namespace=spec.namespace)

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        # TokenReview proves the sidecar's projected, audience-scoped ServiceAccount
        # token and names the Pod it is bound to. Creating a review grants nothing of
        # the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{self.spec.namespace}-egress-token-reviewer",
            service_account_name=_NAME,
            namespace=self.spec.namespace,
        )
        # What the proxy reads to decide a request: policies, bindings, credentials.
        # No Pods (TokenReview already names the subject) and no Secrets (an
        # EgressCredential names one, but the values live in a separate namespace
        # this Role doesn't grant).
        Role(
            self,
            "role",
            metadata=metadata(_NAME, self.spec.namespace),
            rules=[
                RolePolicyRule(
                    resources=[
                        _custom("agentplane.allegedly.works", resource)
                        for resource in ["egresspolicies", "egressbindings", "egresscredentials"]
                    ],
                    verbs=["get", "list", "watch"],
                )
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=metadata(_NAME, self.spec.namespace),
            role=Role.from_role_name(self, "role-ref", _NAME),
        ).add_subjects(service_account)

    def _add_certificate_and_bundle(self) -> None:
        # The interception root the proxy issues leaves from, separate from the
        # cluster's internal CA (the haku-egress-proxy pattern). Reflected into
        # cert-manager, trust-manager's source namespace, so the Bundle below can
        # publish it to the runner Pods.
        Certificate(
            self,
            "certificate",
            metadata=metadata("agentplane-egress-root-ca", self.spec.namespace),
            spec=CertificateSpec(
                is_ca=True,
                common_name="agentplane-egress-root-ca",
                secret_name=self.spec.ca_secret_name,
                duration="87600h",  # 10 years
                renew_before="8760h",  # 1 year
                private_key=CertificateSpecPrivateKey(algorithm=CertificateSpecPrivateKeyAlgorithm.ECDSA, size=256),
                secret_template=CertificateSpecSecretTemplate(
                    annotations={
                        "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                        "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "cert-manager",
                        "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                        "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "cert-manager",
                    }
                ),
                issuer_ref=CertificateSpecIssuerRef(name=_ROOT_CA_ISSUER, kind="ClusterIssuer"),
            ),
        )
        # Public roots + cluster root + the proxy's interception root, written as a
        # ConfigMap of the same name, where the runner SandboxTemplate mounts it over
        # the runner container's system bundle.
        Bundle(
            self,
            "bundle",
            metadata=ApiObjectMetadata(name=self.spec.ca_secret_name),
            spec=BundleSpec(
                sources=[
                    BundleSpecSources(use_default_c_as=True),
                    BundleSpecSources(secret=BundleSpecSourcesSecret(name="cluster-root-ca-secret", key="ca.crt")),
                    BundleSpecSources(secret=BundleSpecSourcesSecret(name=self.spec.ca_secret_name, key="tls.crt")),
                ],
                target=BundleSpecTarget(
                    config_map=BundleSpecTargetConfigMap(
                        key="ca-certificates.crt",
                        metadata=BundleSpecTargetConfigMapMetadata(
                            annotations={
                                "description": (
                                    f"Trust bundle for {self.spec.namespace} runner HTTPS traffic "
                                    "intercepted by the egress proxy"
                                )
                            }
                        ),
                    ),
                    namespace_selector=BundleSpecTargetNamespaceSelector(
                        match_expressions=[
                            BundleSpecTargetNamespaceSelectorMatchExpressions(
                                key="kubernetes.io/metadata.name", operator="In", values=[self.spec.namespace]
                            )
                        ]
                    ),
                ),
            ),
        )

    def _add_settings_configmap(self) -> ConfigMap:
        return ConfigMap(
            self,
            "settings",
            metadata=metadata(f"{_NAME}-settings", self.spec.namespace),
            data={"settings.yaml": yaml_config({"allowed_service_account_namespaces": [self.spec.namespace]})},
        )

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        ca_secret = Secret.from_secret_name(self, "ca-secret-ref", self.spec.ca_secret_name)
        ca_volume = Volume.from_secret(self, "ca-volume", ca_secret, name="ca")
        confdir_volume = Volume.from_empty_dir(self, "confdir-volume", "confdir")

        migrate_env = {
            "AGENTPLANE_EGRESS_DATABASE_URL": EnvValue.from_secret_value(
                SecretValue(
                    secret=Secret.from_secret_name(self, "postgres-egress-secret", "postgres-egress"), key="uri"
                )
            )
        }

        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME, self.spec.namespace, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.spec.replicas,
            strategy=self.spec.strategy,
            min_ready=Duration.seconds(self.spec.min_ready_seconds)
            if self.spec.min_ready_seconds is not None
            else None,
            termination_grace_period=Duration.seconds(60),
            service_account=service_account,
            automount_service_account_token=True,
            docker_registry_auth=Secret.from_secret_name(self, "forgejo-images-creds-ref", "forgejo-images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[
                ContainerProps(
                    name="migrate",
                    image=f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}",
                    image_pull_policy=ImagePullPolicy.ALWAYS,
                    env_variables=migrate_env,
                    resources=ContainerResources(
                        cpu=CpuResources(request=Cpu.millis(25)),
                        memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(256)),
                    ),
                    security_context=ContainerSecurityContextProps(
                        allow_privilege_escalation=False,
                        capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                        read_only_root_filesystem=False,
                    ),
                )
            ],
        )
        deployment.add_container(
            name="proxy",
            image=f"{_PROXY_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=[
                f"--rules-namespace={self.spec.namespace}",
                "--credentials-namespace=agentplane-egress-credentials",
                f"--listen-port={_PROXY_PORT}",
                f"--admin-port={_ADMIN_PORT}",
                f"--agent-api-port={_AGENT_API_PORT}",
                "--ca-cert=/etc/agentplane-egress/ca/tls.crt",
                "--ca-key=/etc/agentplane-egress/ca/tls.key",
                "--confdir=/var/lib/agentplane-egress",
                "--token-audience=agentplane-egress",
            ],
            env_variables={
                "AGENTPLANE_EGRESS_DATABASE_URL": EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "postgres-egress-secret-proxy", "postgres-egress"),
                        key="uri",
                    )
                ),
                # Settings this deployment supplies as YAML rather than flags, so a list is a list.
                "AGENTPLANE_EGRESS_CONFIG_FILE": EnvValue.from_value("/etc/agentplane-egress/settings.yaml"),
            },
            ports=[
                ContainerPort(name="proxy", number=_PROXY_PORT, protocol=Protocol.TCP),
                ContainerPort(name="admin", number=_ADMIN_PORT, protocol=Protocol.TCP),
                ContainerPort(name="agent-api", number=_AGENT_API_PORT, protocol=Protocol.TCP),
            ],
            readiness=http_probe("/healthz", port=_ADMIN_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/livez", port=_ADMIN_PORT, initial_delay_seconds=30, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            security_context=ContainerSecurityContextProps(
                allow_privilege_escalation=False,
                capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
                read_only_root_filesystem=False,
            ),
        )
        deployment.containers[0].mount("/etc/agentplane-egress/ca", ca_volume, read_only=True)
        deployment.containers[0].mount("/var/lib/agentplane-egress", confdir_volume)
        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount(
            "/etc/agentplane-egress/settings.yaml", settings_volume, sub_path="settings.yaml", read_only=True
        )

        deployment.scheduling.attract(Node.labeled(NodeLabelQuery.is_("topology.kubernetes.io/zone", _ZONE)))
        deployment.scheduling.tolerate(
            Node.tainted(NodeTaintQuery.exists("node-role.kubernetes.io/control-plane", effect=TaintEffect.NO_SCHEDULE))
        )

        # cdk8s_plus_34's PodSecurityContextProps has no seccompProfile builder --
        # patch the pod-level field directly (same escape hatch used elsewhere for
        # this exact gap; see llm_ingress_constructs.py).
        pod_spec_patches = [
            JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", {"type": "RuntimeDefault"})
        ]
        if self.spec.topology_spread:
            pod_spec_patches.append(
                JsonPatch.add(
                    "/spec/template/spec/topologySpreadConstraints",
                    [
                        k8s.TopologySpreadConstraint(
                            max_skew=1,
                            topology_key="kubernetes.io/hostname",
                            when_unsatisfiable="ScheduleAnyway",
                            label_selector=k8s.LabelSelector(match_labels=_LABELS),
                        )
                    ],
                )
            )
        for patch in pod_spec_patches:
            ApiObject.of(deployment).add_json_patch(patch)
        return deployment

    def _add_services(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.spec.namespace),
            selector=deployment,
            ports=[
                ServicePort(name="http", port=80, target_port=_AGENT_API_PORT, protocol=Protocol.TCP),
                ServicePort(name="proxy", port=_PROXY_PORT, target_port=_PROXY_PORT, protocol=Protocol.TCP),
            ],
        )
        Service(
            self,
            "service-admin",
            metadata=metadata(f"{_NAME}-admin", self.spec.namespace),
            selector=deployment,
            ports=[ServicePort(name="admin", port=_ADMIN_PORT, target_port=_ADMIN_PORT, protocol=Protocol.TCP)],
        )

    def _add_pdb(self) -> None:
        k8s.KubePodDisruptionBudget(
            self,
            "pdb",
            metadata=k8s.ObjectMeta(name=_NAME, namespace=self.spec.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(1), selector=k8s.LabelSelector(match_labels=_LABELS)
            ),
        )

    def _add_network_policy(self) -> None:
        namespace = self.spec.namespace
        CiliumNetworkPolicy(
            self,
            "networkpolicy",
            metadata=metadata(_NAME, namespace),
            spec=CiliumNetworkPolicySpec(
                endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_LABELS),
                ingress=[
                    CiliumNetworkPolicySpecIngress(
                        from_endpoints=[
                            CiliumNetworkPolicySpecIngressFromEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-runner",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port=str(_PROXY_PORT),
                                        protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecIngress(
                        from_endpoints=[
                            CiliumNetworkPolicySpecIngressFromEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "app.kubernetes.io/name": "agentplane-app",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port=str(_ADMIN_PORT),
                                        protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecIngress(
                        from_endpoints=[
                            CiliumNetworkPolicySpecIngressFromEndpoints(match_labels=_endpoint_labels(namespace, _NAME))
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecIngressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecIngressToPortsPorts(
                                        port=str(_AGENT_API_PORT),
                                        protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    ),
                ],
                egress=[
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={
                                    "k8s:io.kubernetes.pod.namespace": namespace,
                                    "k8s:cnpg.io/cluster": "postgres",
                                }
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="5432", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels={"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="53", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.ANY
                                    )
                                ],
                                rules=CiliumNetworkPolicySpecEgressToPortsRules(
                                    dns=[CiliumNetworkPolicySpecEgressToPortsRulesDns(match_pattern="*")]
                                ),
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_entities=[CiliumNetworkPolicySpecEgressToEntities.KUBE_HYPHEN_APISERVER]
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(match_labels=_endpoint_labels(namespace, _NAME))
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port=str(_AGENT_API_PORT),
                                        protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP,
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels=_endpoint_labels(namespace, "agentplane-llm-ingress")
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="8080", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_endpoints=[
                            CiliumNetworkPolicySpecEgressToEndpoints(
                                match_labels=_endpoint_labels(namespace, "agentplane-actions")
                            )
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="8080", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    )
                                ]
                            )
                        ],
                    ),
                    CiliumNetworkPolicySpecEgress(
                        to_entities=[
                            CiliumNetworkPolicySpecEgressToEntities.WORLD,
                            CiliumNetworkPolicySpecEgressToEntities.REMOTE_HYPHEN_NODE,
                            CiliumNetworkPolicySpecEgressToEntities.HOST,
                        ],
                        to_ports=[
                            CiliumNetworkPolicySpecEgressToPorts(
                                ports=[
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="443", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    ),
                                    CiliumNetworkPolicySpecEgressToPortsPorts(
                                        port="80", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                    ),
                                ]
                            )
                        ],
                    ),
                ],
            ),
        )


def _endpoint_labels(namespace: str, name: str) -> dict[str, str]:
    return {"k8s:io.kubernetes.pod.namespace": namespace, "app.kubernetes.io/name": name}
