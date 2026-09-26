"""The central egress proxy, its interception CA/trust bundle, and the
EgressCredential/EgressPolicy resources it reads.
"""

from __future__ import annotations

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredential,
    EgressCredentialSpec,
    EgressCredentialSpecSource,
    EgressCredentialSpecSourceProjectedWorkloadToken,
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
)
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
    Bundle,
    BundleSpec,
    BundleSpecSources,
    BundleSpecSourcesConfigMap,
    BundleSpecTarget,
    BundleSpecTargetAdditionalFormats,
    BundleSpecTargetAdditionalFormatsPkcs12,
    BundleSpecTargetConfigMap,
    BundleSpecTargetConfigMapMetadata,
    BundleSpecTargetNamespaceSelector,
    BundleSpecTargetNamespaceSelectorMatchExpressions,
)

from agentplane.egress.database_migrate import MigrationSettings
from agentplane.egress.main import CONFIG_FILE_ENV, Settings
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import actions, container_security, database, llm_ingress, node_scheduling
from cluster.cdk8s.agentplane.app_settings import BASIC_POLICY, GITHUB_PUBLIC_POLICY, KUBERNETES_POLICY, PACKAGES_POLICY
from cluster.cdk8s.agentplane.egress_credentials import GITHUB_PAT_SECRET
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name, settings_file

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "agentplane-egress"
_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-migrate"
_LABELS = {"app.kubernetes.io/name": NAME}
PROXY_PORT = 8888
ADMIN_PORT = 8081
_AGENT_API_PORT = 8082
# The audience the API server validates its own ServiceAccount tokens against. Read off this
# cluster on 2026-09-19: Talos sets both `--api-audiences` and `--service-account-issuer` to this
# on every kube-apiserver static pod. It is an issuer identifier and not an address anything dials,
# so `localhost` here is not a mistake and not reachable -- do not "correct" it to the Service DNS
# name, which is what the API server would then refuse. The sidecar's projection asks for exactly
# this string and the proxy reviews against it, so a value that does not match the cluster yields a
# 401 inside the box rather than a proxy denial. Changing it means changing the cluster.
KUBERNETES_AUDIENCE = "https://localhost:7445"
# Where a sandbox's kubectl sends everything. Cluster-internal by definition, hence the rule below.
KUBERNETES_HOST = "kubernetes.default.svc.cluster.local"
# The credential substituted there, whose placeholder a sandbox's kubeconfig carries (sandbox_pod.py).
KUBERNETES_CREDENTIAL = "kubernetes-workload"
# The in-cluster Forgejo, not `git.allegedly.works`: the public name would hairpin out through
# the Gateway and back for a Service one hop away. Plain HTTP on 3000, so the proxy reads the
# request without bumping TLS.
FORGEJO_HOST = "forgejo-http.forgejo.svc.cluster.local"
FORGEJO_PORT = 3000
# Home Assistant's in-cluster Service, plain HTTP. Its pod runs on its node's host network, so
# Cilium sees a node there, not an endpoint: the proxy's rule for it is an entity rule on its port.
HOME_ASSISTANT_HOST = "home-assistant.home-assistant.svc.cluster.local"
HOME_ASSISTANT_PORT = 8123
_SETTINGS_PATH = "/etc/agentplane-egress/settings.yaml"
# The trust bundles' ConfigMap key.
CA_BUNDLE_KEY = "ca-certificates.crt"
# The sandbox bundle's roots again, as the PKCS12 trust store a JVM reads.
JAVA_TRUST_STORE_KEY = "ca-certificates.p12"
_UPSTREAM_CA_DIR = "/etc/agentplane-egress/upstream-ca"


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
                secret_ref=EgressCredentialSpecSourceSecretRef(name=GITHUB_PAT_SECRET, key="token")
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

    EgressCredential(
        scope,
        "egresscredential-kubernetes-workload",
        metadata=ApiObjectMetadata(name=KUBERNETES_CREDENTIAL, namespace=namespace),
        spec=EgressCredentialSpec(
            description=(
                "The calling Sandbox Pod's own ServiceAccount, minted for the Kubernetes API server "
                "rather than for this proxy. Requests carrying it are authorized by the API server "
                "as that account and by nothing here: what the sandbox may do is the RBAC bound to "
                "it, and this proxy adds only the rule's hosts, methods and paths on top."
            ),
            source=EgressCredentialSpecSource(
                projected_workload_token=EgressCredentialSpecSourceProjectedWorkloadToken(audience=KUBERNETES_AUDIENCE)
            ),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
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
        "egresspolicy-kubernetes",
        metadata=ApiObjectMetadata(name=KUBERNETES_POLICY, namespace=namespace),
        spec=EgressPolicySpec(
            rules=[
                # No method or path list: what a sandbox may read or write is the API server's
                # answer for its own ServiceAccount, and narrowing verbs here would be a second,
                # weaker copy of RBAC that drifts from it. Upgrade verbs (exec, attach,
                # port-forward) negotiate SPDY or WebSocket through an intercepting proxy and are
                # not known to work; ordinary requests and watches are what this admits in practice.
                EgressPolicySpecRules(
                    hosts=[KUBERNETES_HOST],
                    cluster_internal=True,
                    credential_ref=EgressPolicySpecRulesCredentialRef(name=KUBERNETES_CREDENTIAL),
                )
            ]
        ),
    )
    EgressPolicy(
        scope,
        "egresspolicy-packages",
        metadata=ApiObjectMetadata(name=PACKAGES_POLICY, namespace=namespace),
        spec=EgressPolicySpec(
            rules=[
                # The package and toolchain mirrors a box needs to install anything: without them
                # `pip install`, `npm install`, a cargo fetch and every Bazel download fail in a
                # sandbox whose whole purpose is running commands. Taken from the set haku's own
                # agent reaches (egress_fences.OPENCLAW_SPIKE_ALLOWLIST).
                #
                # No credentialRef: these are public, unauthenticated reads, so there is nothing to
                # substitute and a compromised box gains no identity here. That is also why the
                # methods are narrowed, unlike the Kubernetes and Forgejo rules -- with no
                # credential behind it, GET and HEAD genuinely bound what this admits rather than
                # bounding the request while the authority stays whole. HEAD is here because an OCI
                # pull checks a manifest with it before fetching.
                #
                # Deliberately absent: `codeload.github.com` and the `objects`/`release-assets`
                # githubusercontent hosts, which are where an `http_archive` of a GitHub tag
                # actually downloads from. They belong with the GitHub policy below, whose rule
                # substitutes a PAT on the same hosts; claude-ai's boxes, which are not bound to
                # it, get them without a credential from `github-downloads`
                # (actions_staging_policies.py).
                EgressPolicySpecRules(
                    hosts=[
                        # keep-sorted start
                        "bcr.bazel.build",
                        "cache.nixos.org",
                        "channels.nixos.org",
                        "code.forgejo.org",
                        "data.forgejo.org",
                        "files.pythonhosted.org",
                        "ftp.gnu.org",
                        "ghcr.io",
                        "index.crates.io",
                        "nixos.org",
                        "nodejs.org",
                        # ghcr.io redirects blob reads here, so a pull fails without it. A
                        # githubusercontent host in this policy rather than the GitHub one because
                        # it carries container layers, not repository content, and needs no token.
                        "pkg-containers.githubusercontent.com",
                        "pypi.org",
                        "registry.npmjs.org",
                        "releases.bazel.build",
                        "snapshot.debian.org",
                        "static.crates.io",
                        "static.rust-lang.org",
                        # keep-sorted end
                    ],
                    methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.HEAD],
                )
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
                    hosts=["api.github.com", "github.com", "codeload.github.com", "*.githubusercontent.com"],
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
        self.upstream_bundle = self._add_upstream_bundle()
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
        # The interception root the proxy issues leaves from, separate from the cluster's
        # internal CA (the haku-egress-proxy pattern). Published as a ConfigMap of the same
        # name, which every sandbox Pod mounts over its system bundle and as its Java trust
        # store (sandbox_pod.py).
        interception_root_ca(
            self,
            name="agentplane-egress-root-ca",
            namespace=self.env.namespace,
            secret_name=self.env.egress.ca_secret_name,
            bundle_name=self.env.egress.ca_secret_name,
            description=f"Trust bundle for {self.env.namespace} runner HTTPS traffic intercepted by the egress proxy",
            target_namespaces=(self.env.namespace,),
            # With no password, trust-manager writes the PKCS12 store with neither encryption
            # nor a MAC, which a JVM loads when it is given no password either.
            additional_formats=BundleSpecTargetAdditionalFormats(
                pkcs12=BundleSpecTargetAdditionalFormatsPkcs12(key=JAVA_TRUST_STORE_KEY)
            ),
        )

    def _add_upstream_bundle(self) -> Bundle:
        """What this proxy verifies destinations against, as distinct from what a runner trusts.

        The runner's bundle carries the interception root, because the proxy is what answers it.
        This one must not: the proxy is that interceptor, and it dials the real destination. What it
        needs instead is the cluster's own CA, since a `clusterInternal` rule reaches the API
        server, whose serving certificate no public root signs.

        `kube-root-ca.crt` is the ConfigMap kube-controller-manager publishes into every namespace,
        and is the Kubernetes CA -- not `cluster-root-ca-secret`, which is cert-manager's own root
        for issuing internal leaves and signs nothing the API server presents.
        """
        return Bundle(
            self,
            "upstream-bundle",
            metadata=ApiObjectMetadata(name=f"{self.env.namespace}-egress-upstream-ca"),
            spec=BundleSpec(
                sources=[
                    BundleSpecSources(use_default_c_as=True),
                    BundleSpecSources(config_map=BundleSpecSourcesConfigMap(name="kube-root-ca.crt", key="ca.crt")),
                ],
                target=BundleSpecTarget(
                    config_map=BundleSpecTargetConfigMap(
                        key=CA_BUNDLE_KEY,
                        metadata=BundleSpecTargetConfigMapMetadata(
                            annotations={
                                "description": (
                                    f"Trust bundle the {self.env.namespace} egress proxy verifies "
                                    "destinations with: public roots plus this cluster's CA"
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
                    settings_file(
                        Settings,
                        {
                            "allowed_service_account_namespaces": [self.env.namespace],
                            "projected_token_audiences": [KUBERNETES_AUDIENCE],
                        },
                    )
                )
            },
        )

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        ca_secret = Secret.from_secret_name(self, "ca-secret-ref", self.env.egress.ca_secret_name)
        ca_volume = Volume.from_secret(self, "ca-volume", ca_secret, name="ca")
        confdir_volume = Volume.from_empty_dir(self, "confdir-volume", "confdir")
        upstream_ca_volume = Volume.from_config_map(
            self,
            "upstream-ca-volume",
            ConfigMap.from_config_map_name(self, "upstream-ca-ref", self.upstream_bundle.name),
            name="upstream-ca",
        )

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
                credentials_namespace=self.env.egress.credentials_namespace,
                listen_port=PROXY_PORT,
                admin_port=ADMIN_PORT,
                agent_api_port=_AGENT_API_PORT,
                ca_cert="/etc/agentplane-egress/ca/tls.crt",
                ca_key="/etc/agentplane-egress/ca/tls.key",
                confdir="/var/lib/agentplane-egress",
                upstream_ca_file=f"{_UPSTREAM_CA_DIR}/{CA_BUNDLE_KEY}",
                token_audience=llm_ingress.WORKLOAD_TOKEN_AUDIENCE,
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
        deployment.containers[0].mount(_UPSTREAM_CA_DIR, upstream_ca_volume, read_only=True)
        deployment.containers[0].mount("/var/lib/agentplane-egress", confdir_volume)
        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount(_SETTINGS_PATH, settings_volume, sub_path="settings.yaml", read_only=True)

        node_scheduling.attract_to_zone(deployment)
        node_scheduling.tolerate_control_plane_taint(deployment)
        apply_pod_spec_patches(deployment)
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
        add_pod_disruption_budget(
            self, "pdb", name=NAME, namespace=self.env.namespace, min_available=min_available, selector=_LABELS
        )

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=metadata(NAME, namespace),
            selector=_LABELS,
            ingress=[
                # Runner Pods, and the sandbox Actions' command boxes (command_sandbox.py, which
                # imports this module).
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-runner"),
                    cilium.endpoint_labels(namespace, "agentplane-sandbox"),
                    ports=[PROXY_PORT],
                ),
                IngressRule.from_endpoints(cilium.endpoint_labels(namespace, "agentplane-app"), ports=[ADMIN_PORT]),
                IngressRule.from_endpoints(cilium.endpoint_labels(namespace, NAME), ports=[_AGENT_API_PORT]),
            ],
            egress=[
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
                cilium.dns_egress(protocols=["ANY"], resolves=["*"]),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(cilium.endpoint_labels(namespace, NAME), _AGENT_API_PORT),
                EgressRule.to_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-llm-ingress"), llm_ingress.CONTAINER_PORT
                ),
                EgressRule.to_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-actions"), actions.CONTAINER_PORT
                ),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": "forgejo", "k8s:app.kubernetes.io/name": "forgejo"},
                    FORGEJO_PORT,
                ),
                EgressRule.to_entities(Entity.REMOTE_NODE, Entity.HOST, ports=[HOME_ASSISTANT_PORT]),
                EgressRule.to_entities(Entity.WORLD, Entity.REMOTE_NODE, Entity.HOST, ports=[443, 80]),
            ],
        )
