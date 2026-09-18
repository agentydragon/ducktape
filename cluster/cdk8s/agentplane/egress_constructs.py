"""The central egress proxy, its interception CA/trust bundle, and the
EgressCredential/EgressPolicy resources it reads.
"""

from __future__ import annotations

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
from cdk8s import ApiObjectMetadata, Duration, Size
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
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
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

from cluster.cdk8s.agentplane import (
    actions_constructs,
    cilium_helpers,
    container_security,
    db_constructs,
    llm_ingress_constructs,
    node_scheduling,
)
from cluster.cdk8s.agentplane.app_settings import BASIC_POLICY, GITHUB_PUBLIC_POLICY
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from x.agentplane.egress.database_migrate import MigrationSettings
from x.agentplane.egress.main import CONFIG_FILE_ENV, Settings
from x.agentplane.settings_contract import cli_args, env_name, settings_file

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "agentplane-egress"
_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-migrate"
_LABELS = {"app.kubernetes.io/name": NAME}
PROXY_PORT = 8888
ADMIN_PORT = 8081
_AGENT_API_PORT = 8082
_ROOT_CA_ISSUER = "cluster-ca-bootstrap"
_SETTINGS_PATH = "/etc/agentplane-egress/settings.yaml"
# The trust bundle's ConfigMap key -- the runner SandboxTemplate's volumeMount subPath
# (app_constructs.py) must name the same key.
CA_BUNDLE_KEY = "ca-certificates.crt"


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
        metadata=ApiObjectMetadata(name=BASIC_POLICY, namespace=namespace),
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
        metadata=ApiObjectMetadata(name=GITHUB_PUBLIC_POLICY, namespace=namespace),
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

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the proxy
        # calls TokenReview as itself, so it needs its own mounted token.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=metadata(NAME, env.namespace), automount_token=True
        )
        self._add_rbac(service_account)
        self._add_certificate_and_bundle()
        settings_cm = self._add_settings_configmap()
        deployment = self._add_deployment(service_account, settings_cm)
        self._add_services(deployment)
        self._add_network_policy()
        if env.replicas.pdb_min_available is not None:
            self._add_pdb(env.replicas.pdb_min_available)
        _egress_credentials(self, namespace=env.namespace)
        _egress_policies(self, namespace=env.namespace)

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        # TokenReview proves the sidecar's projected, audience-scoped ServiceAccount
        # token and names the Pod it is bound to. Creating a review grants nothing of
        # the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{self.env.namespace}-egress-token-reviewer",
            service_account_name=NAME,
            namespace=self.env.namespace,
        )
        # What the proxy reads to decide a request: policies, bindings, credentials.
        # No Pods (TokenReview already names the subject) and no Secrets (an
        # EgressCredential names one, but the values live in a separate namespace
        # this Role doesn't grant).
        Role(
            self,
            "role",
            metadata=metadata(NAME, self.env.namespace),
            rules=[
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ["egresspolicies", "egressbindings", "egresscredentials"]
                    ],
                    verbs=["get", "list", "watch"],
                )
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=metadata(NAME, self.env.namespace),
            role=Role.from_role_name(self, "role-ref", NAME),
        ).add_subjects(service_account)

    def _add_certificate_and_bundle(self) -> None:
        # The interception root the proxy issues leaves from, separate from the
        # cluster's internal CA (the haku-egress-proxy pattern). Reflected into
        # cert-manager, trust-manager's source namespace, so the Bundle below can
        # publish it to the runner Pods.
        Certificate(
            self,
            "certificate",
            metadata=metadata("agentplane-egress-root-ca", self.env.namespace),
            spec=CertificateSpec(
                is_ca=True,
                common_name="agentplane-egress-root-ca",
                secret_name=self.env.egress.ca_secret_name,
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
            metadata=ApiObjectMetadata(name=self.env.egress.ca_secret_name),
            spec=BundleSpec(
                sources=[
                    BundleSpecSources(use_default_c_as=True),
                    BundleSpecSources(secret=BundleSpecSourcesSecret(name="cluster-root-ca-secret", key="ca.crt")),
                    BundleSpecSources(
                        secret=BundleSpecSourcesSecret(name=self.env.egress.ca_secret_name, key="tls.crt")
                    ),
                ],
                target=BundleSpecTarget(
                    config_map=BundleSpecTargetConfigMap(
                        key=CA_BUNDLE_KEY,
                        metadata=BundleSpecTargetConfigMapMetadata(
                            annotations={
                                "description": (
                                    f"Trust bundle for {self.env.namespace} runner HTTPS traffic "
                                    "intercepted by the egress proxy"
                                )
                            }
                        ),
                    ),
                    namespace_selector=BundleSpecTargetNamespaceSelector(
                        match_expressions=[
                            BundleSpecTargetNamespaceSelectorMatchExpressions(
                                key="kubernetes.io/metadata.name", operator="In", values=[self.env.namespace]
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
            metadata=metadata(f"{NAME}-settings", self.env.namespace),
            data={
                "settings.yaml": yaml_config(
                    settings_file(Settings, {"allowed_service_account_namespaces": [self.env.namespace]})
                )
            },
        )

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        ca_secret = Secret.from_secret_name(self, "ca-secret-ref", self.env.egress.ca_secret_name)
        ca_volume = Volume.from_secret(self, "ca-volume", ca_secret, name="ca")
        confdir_volume = Volume.from_empty_dir(self, "confdir-volume", "confdir")

        migrate_env = {
            env_name(MigrationSettings, "database_url"): EnvValue.from_secret_value(
                SecretValue(
                    secret=Secret.from_secret_name(self, "postgres-egress-secret", "postgres-egress"), key="uri"
                )
            )
        }

        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                NAME, self.env.namespace, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.env.replicas.count,
            strategy=self.env.replicas.strategy,
            min_ready=self.env.replicas.min_ready,
            termination_grace_period=Duration.seconds(60),
            service_account=service_account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[migrate_init_container(f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}", env_variables=migrate_env)],
        )
        deployment.add_container(
            name="proxy",
            image=f"{_PROXY_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=cli_args(
                Settings,
                rules_namespace=self.env.namespace,
                credentials_namespace="agentplane-egress-credentials",
                listen_port=PROXY_PORT,
                admin_port=ADMIN_PORT,
                agent_api_port=_AGENT_API_PORT,
                ca_cert="/etc/agentplane-egress/ca/tls.crt",
                ca_key="/etc/agentplane-egress/ca/tls.key",
                confdir="/var/lib/agentplane-egress",
                token_audience=llm_ingress_constructs.WORKLOAD_TOKEN_AUDIENCE,
            ),
            env_variables={
                env_name(Settings, "database_url"): EnvValue.from_secret_value(
                    SecretValue(
                        secret=Secret.from_secret_name(self, "postgres-egress-secret-proxy", "postgres-egress"),
                        key="uri",
                    )
                ),
                # Settings this deployment supplies as YAML rather than flags, so a list is a list.
                CONFIG_FILE_ENV: EnvValue.from_value(_SETTINGS_PATH),
            },
            ports=[
                ContainerPort(name="proxy", number=PROXY_PORT, protocol=Protocol.TCP),
                ContainerPort(name="admin", number=ADMIN_PORT, protocol=Protocol.TCP),
                ContainerPort(name="agent-api", number=_AGENT_API_PORT, protocol=Protocol.TCP),
            ],
            readiness=http_probe("/healthz", port=ADMIN_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/livez", port=ADMIN_PORT, initial_delay_seconds=30, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )
        deployment.containers[0].mount("/etc/agentplane-egress/ca", ca_volume, read_only=True)
        deployment.containers[0].mount("/var/lib/agentplane-egress", confdir_volume)
        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount(_SETTINGS_PATH, settings_volume, sub_path="settings.yaml", read_only=True)

        node_scheduling.attract_to_zone(deployment)
        node_scheduling.tolerate_control_plane_taint(deployment)
        apply_pod_spec_patches(deployment, labels=_LABELS, topology_spread=self.env.replicas.topology_spread)
        return deployment

    def _add_services(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(NAME, self.env.namespace),
            selector=deployment,
            ports=[
                ServicePort(name="http", port=80, target_port=_AGENT_API_PORT, protocol=Protocol.TCP),
                ServicePort(name="proxy", port=PROXY_PORT, target_port=PROXY_PORT, protocol=Protocol.TCP),
            ],
        )
        Service(
            self,
            "service-admin",
            metadata=metadata(f"{NAME}-admin", self.env.namespace),
            selector=deployment,
            ports=[ServicePort(name="admin", port=ADMIN_PORT, target_port=ADMIN_PORT, protocol=Protocol.TCP)],
        )

    def _add_pdb(self, min_available: int) -> None:
        k8s.KubePodDisruptionBudget(
            self,
            "pdb",
            metadata=k8s.ObjectMeta(name=NAME, namespace=self.env.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(min_available),
                selector=k8s.LabelSelector(match_labels=_LABELS),
            ),
        )

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        cilium_helpers.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(NAME, namespace),
            selector=_LABELS,
            ingress=[
                cilium_helpers.ingress_from(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-runner"), ports=[PROXY_PORT]
                ),
                cilium_helpers.ingress_from(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-app"), ports=[ADMIN_PORT]
                ),
                cilium_helpers.ingress_from(cilium_helpers.endpoint_labels(namespace, NAME), ports=[_AGENT_API_PORT]),
            ],
            egress=[
                cilium_helpers.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    db_constructs.POSTGRES_PORT,
                ),
                cilium_helpers.dns_egress(protocols=["ANY"], l7=True),
                cilium_helpers.egress_to_entities("kube-apiserver"),
                cilium_helpers.egress_to(cilium_helpers.endpoint_labels(namespace, NAME), _AGENT_API_PORT),
                cilium_helpers.egress_to(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-llm-ingress"),
                    llm_ingress_constructs.CONTAINER_PORT,
                ),
                cilium_helpers.egress_to(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-actions"), actions_constructs.CONTAINER_PORT
                ),
                cilium_helpers.egress_to_entities("world", "remote-node", "host", ports=[443, 80]),
            ],
        )
