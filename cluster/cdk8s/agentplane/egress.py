"""The central egress proxy, its interception CA/trust bundle, and the
EgressCredential/EgressPolicy resources it reads.
"""

from __future__ import annotations

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerPort,
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Protocol,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from constructs import Construct
from trust_manager_crds.io.cert_manager.trust import (
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
from agentplane.egress.settings import CONFIG_FILE_ENV, Settings
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import actions, database, llm_ingress, notifications
from cluster.cdk8s.agentplane.egress_credentials import BUILDBUDDY_API_KEY_SECRET, GITHUB_PAT_SECRET
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.forgejo import app as forgejo  # a bare `app.HTTP` would not say whose
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
from cluster.cdk8s.home_assistant import app as home_assistant  # a bare `app.SERVICE` would not say whose
from cluster.cdk8s.litellm import proxy as litellm_proxy
from cluster.cdk8s.litellm.credentials import CHEAP_EXPERIMENTS_KEY
from cluster.cdk8s.ollama import app as ollama
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "agentplane-egress"
_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-migrate"
_LABELS = {"app.kubernetes.io/name": NAME}
PROXY_PORT = 8888
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
# The in-cluster Forgejo, plain HTTP, so the proxy reads the request without bumping TLS.
# It is the name to prefer: the public name below would hairpin out through the Gateway and back
# for a Service one hop away.
FORGEJO_HOST = forgejo.HTTP.fqdn
# The same Service under the shorter names its search path resolves (haku-egress-proxy's clients
# still spell it `forgejo-http.forgejo`). The proxy matches a request's host on the exact string,
# so a spelling left out is refused `no-rule` although it dials the same address.
FORGEJO_HOST_ALIASES = (forgejo.HTTP.host, f"{forgejo.HTTP.name}.{forgejo.HTTP.pods.namespace}")
# Forgejo by its public name: HTTPS through the Gateway, resolving to public addresses.
FORGEJO_PUBLIC_HOST = "git.allegedly.works"
# Home Assistant's in-cluster Service, plain HTTP. The proxy matches requests on this exact string.
HOME_ASSISTANT_HOST = home_assistant.SERVICE.fqdn
# The trust bundles' ConfigMap key.
CA_BUNDLE_KEY = "ca-certificates.crt"
# The sandbox bundle's roots again, as the PKCS12 trust store a JVM reads.
JAVA_TRUST_STORE_KEY = "ca-certificates.p12"
_UPSTREAM_CA_DIR = "/etc/agentplane-egress/upstream-ca"

# The EgressPolicy objects created below that every environment ships. A name is exported
# because a preset or an explicit grant refers to the policy by name; each lives with the
# construct that creates it, so renaming or dropping a policy is a change in one file.
BASIC_POLICY = "basic"
PACKAGES_POLICY = "packages"
INFERENCE_EXPERIMENTS_POLICY = "inference-experiments"
GITHUB_AGENTYDRAGON_AGENT_POLICY = "github-agentydragon-agent"
GITHUB_CLONE_POLICY = "github-clone"
GITHUB_ACTIONS_LOGS_POLICY = "github-actions-logs"
BUILDBUDDY_POLICY = "buildbuddy"
PUBLIC_CODER_VISUALS_POLICY = "public-coder-pr-visuals"
PUBLIC_INTERNET_POLICY = "public-internet"


def _pods(namespace: str) -> Pods:
    return Pods(namespace=namespace, labels=tuple(_LABELS.items()))


def proxy(namespace: str) -> ServiceRef:
    """The forward proxy every box sends its traffic through."""
    return ServiceRef(name=NAME, port=Port(name="proxy", number=PROXY_PORT), pods=_pods(namespace))


def agent_api(namespace: str) -> ServiceRef:
    """The API agents read their rules from, on the proxy's Service."""
    return ServiceRef(name=NAME, port=Port(name="http", number=80), pods=_pods(namespace), target_port=8082)


def admin(namespace: str) -> ServiceRef:
    """The admin API the app calls, on a Service of its own."""
    return ServiceRef(name=f"{NAME}-admin", port=Port(name="admin", number=8081), pods=_pods(namespace))


def _egress_credentials(scope: Construct, *, namespace: str) -> None:
    EgressCredential(
        scope,
        "egresscredential-agentplane-workload",
        metadata=ApiObjectMetadata(name="agentplane-workload", namespace=namespace),
        description=(
            "The calling Sandbox Pod's short-lived, Pod-bound workload identity for first-party "
            "Agentplane destinations. It conveys no LiteLLM credential or operator, Agent, or "
            "Thread authority."
        ),
        source=Source.authenticated_workload_token(),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressCredential(
        scope,
        "egresscredential-notifications-workload",
        metadata=ApiObjectMetadata(name=notifications.WORKLOAD_CREDENTIAL, namespace=namespace),
        description=(
            "The calling Sandbox Pod's short-lived, Pod-bound identity minted specifically for the "
            "Agentplane Notifications API audience."
        ),
        source=Source.projected_workload_token(audience=notifications.TOKEN_AUDIENCE),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    EgressCredential(
        scope,
        "egresscredential-github-pat",
        metadata=ApiObjectMetadata(name="github-pat", namespace=namespace),
        description=(
            "A GitHub personal access token belonging to the bot account agentydragon-agent. "
            "Requests carrying it act as that account and are attributable to it. The proxy does "
            "not narrow what the token itself may do — the rule's hosts and methods are the only "
            "limit it adds, so treat anything the token can reach on those hosts as reachable."
        ),
        source=Source.secret_ref(name=GITHUB_PAT_SECRET, key="token"),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            ),
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD
            ),
        ],
    )

    EgressCredential(
        scope,
        "egresscredential-buildbuddy",
        metadata=ApiObjectMetadata(name=BUILDBUDDY_POLICY, namespace=namespace),
        description=(
            "The shared BuildBuddy API key, copied into this environment's isolated egress-credentials "
            "namespace by ESO. BuildBuddy uses this key for its JSON-over-HTTP API and gRPC services, "
            "presented as the literal value of the `x-buildbuddy-api-key` header/metadata entry. Its "
            "authority is the account's BuildBuddy permissions; the egress policy limits where it is sent."
        ),
        source=Source.secret_ref(name=BUILDBUDDY_API_KEY_SECRET, key="api-key"),
        targets=[
            EgressCredentialSpecTargets(
                header="x-buildbuddy-api-key", method=EgressCredentialSpecTargetsMethod.WHOLE_VALUE
            )
        ],
    )

    for name, source, description in (
        (
            "ollama",
            ollama.DIRECT_TOKEN,
            f"Direct Ollama inference and model metadata at http://{ollama.AUTH_PROXY.fqdn}:{ollama.AUTH_PROXY.port.number}. "
            "Uses the existing token-authenticated proxy, not the unauthenticated Ollama port. "
            "Shared local GPUs; avoid unbounded or concurrent load experiments.",
        ),
        (
            "litellm-cheap-experiments",
            CHEAP_EXPERIMENTS_KEY,
            "The shared LiteLLM cheap-experiments virtual key at http://litellm.litellm.svc.cluster.local:4000. "
            "LiteLLM enforces its model allowlist and shared spending budget; this is not an admin key.",
        ),
    ):
        EgressCredential(
            scope,
            f"egresscredential-{name}",
            metadata=ApiObjectMetadata(name=name, namespace=namespace),
            description=description,
            source=Source.secret_ref(name=source.secret.name, key=source.key),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        )

    EgressCredential(
        scope,
        "egresscredential-kubernetes-workload",
        metadata=ApiObjectMetadata(name=KUBERNETES_CREDENTIAL, namespace=namespace),
        description=(
            "The calling Sandbox Pod's own ServiceAccount, minted for the Kubernetes API server "
            "rather than for this proxy. Requests carrying it are authorized by the API server "
            "as that account and by nothing here: what the sandbox may do is the RBAC bound to "
            "it, and this proxy adds only the rule's hosts, methods and paths on top."
        ),
        source=Source.projected_workload_token(audience=KUBERNETES_AUDIENCE),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )


def _egress_policies(scope: Construct, *, namespace: str) -> None:
    (litellm_spec,) = litellm_proxy.proxy_specs()
    litellm_service = litellm_proxy.service(litellm_spec)
    EgressPolicy(
        scope,
        "egresspolicy-inference-experiments",
        metadata=ApiObjectMetadata(name=INFERENCE_EXPERIMENTS_POLICY, namespace=namespace),
        rules=[
            EgressPolicySpecRules(
                hosts=[ollama.AUTH_PROXY.fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/api/tags", "/api/ps", "/api/version", "/v1/models"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="ollama"),
            ),
            EgressPolicySpecRules(
                hosts=[ollama.AUTH_PROXY.fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=[
                    "/api/show",
                    "/api/chat",
                    "/api/generate",
                    "/api/embed",
                    "/api/embeddings",
                    "/v1/chat/completions",
                    "/v1/completions",
                    "/v1/responses",
                    "/v1/embeddings",
                ],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="ollama"),
            ),
            EgressPolicySpecRules(
                hosts=[litellm_service.fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/v1/models", "/models", "/model/info"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="litellm-cheap-experiments"),
            ),
            EgressPolicySpecRules(
                hosts=[litellm_service.fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=[
                    "/v1/chat/completions",
                    "/v1/completions",
                    "/v1/responses",
                    "/v1/messages",
                    "/v1/messages/count_tokens",
                    "/v1/embeddings",
                    "/v1/audio/transcriptions",
                ],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="litellm-cheap-experiments"),
            ),
        ],
    )
    # Available for explicit grants only: no defaults, presets or standing bindings opt in.
    EgressPolicy(
        scope,
        "public-internet-policy",
        metadata=ApiObjectMetadata(name=PUBLIC_INTERNET_POLICY, namespace=namespace),
        rules=[EgressPolicySpecRules(hosts=["*"])],
    )
    EgressPolicy(
        scope,
        "egresspolicy-basic",
        metadata=ApiObjectMetadata(name=BASIC_POLICY, namespace=namespace),
        rules=[
            # The proxy matches a request's host on the exact string, so the in-cluster hosts are the
            # full names their clients spell.
            EgressPolicySpecRules(
                hosts=[llm_ingress.service(namespace).fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
            ),
            EgressPolicySpecRules(
                hosts=[notifications.service(namespace).fqdn],
                cluster_internal=True,
                methods=[
                    EgressPolicySpecRulesMethods.GET,
                    EgressPolicySpecRulesMethods.POST,
                    EgressPolicySpecRulesMethods.PUT,
                    EgressPolicySpecRulesMethods.PATCH,
                    EgressPolicySpecRulesMethods.DELETE,
                ],
                paths=[
                    "/openapi.json",
                    "/v1/sources",
                    "/v1/subscriptions",
                    "/v1/subscriptions/**",
                    "/v1/inboxes",
                    "/v1/inboxes/**",
                ],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=notifications.WORKLOAD_CREDENTIAL),
            ),
            EgressPolicySpecRules(
                hosts=[actions.service(namespace).fqdn],
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
                hosts=[agent_api(namespace).fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/openapi.json", "/v1/rules"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
            ),
            # The API server, inside `basic` rather than behind a policy a launch opts into: every
            # agent talks to Kubernetes, so what decides it is RBAC and not whether a preset or a
            # caller happened to name a policy. Every SandboxTemplate already mounts the kubeconfig
            # naming this credential (sandbox_pod.py), so a box without this rule held a config
            # whose requests the proxy refused for want of a rule -- a transport gap that read as
            # an authorization answer and could not be narrowed into one.
            #
            # No method or path list: what a sandbox may read or write is the API server's answer
            # for its own ServiceAccount, and narrowing verbs here would be a second, weaker copy
            # of RBAC that drifts from it. Upgrade verbs (exec, attach, port-forward) negotiate
            # SPDY or WebSocket through an intercepting proxy and are not known to work; ordinary
            # requests and watches are what this admits in practice.
            EgressPolicySpecRules(
                hosts=[KUBERNETES_HOST],
                cluster_internal=True,
                credential_ref=EgressPolicySpecRulesCredentialRef(name=KUBERNETES_CREDENTIAL),
            ),
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-packages",
        metadata=ApiObjectMetadata(name=PACKAGES_POLICY, namespace=namespace),
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
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-github-agentydragon-agent",
        metadata=ApiObjectMetadata(name=GITHUB_AGENTYDRAGON_AGENT_POLICY, namespace=namespace),
        rules=[
            EgressPolicySpecRules(
                hosts=["api.github.com", "github.com", "codeload.github.com", "*.githubusercontent.com"],
                methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="github-pat"),
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-github-actions-logs",
        metadata=ApiObjectMetadata(name=GITHUB_ACTIONS_LOGS_POLICY, namespace=namespace),
        rules=[
            # A workflow run's job logs (`GET .../actions/jobs/{id}/logs`) and artifacts
            # (`GET .../actions/artifacts/{id}/zip`) answer from api.github.com with a 302 to
            # a presigned Azure Blob Storage URL rather than the log bytes themselves; without
            # this, following that redirect fails and a sandbox reviewing its own PR's CI
            # cannot read why a check failed. The URL's SAS token is in the query string, not
            # a header, so there is nothing for the PAT substitution to attach and no
            # credentialRef here -- this is the same shape as `packages` below. GET-only:
            # retrieving a log or artifact archive, never uploading one.
            EgressPolicySpecRules(hosts=["*.blob.core.windows.net"], methods=[EgressPolicySpecRulesMethods.GET])
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-public-coder-pr-visuals",
        metadata=ApiObjectMetadata(name=PUBLIC_CODER_VISUALS_POLICY, namespace=namespace),
        rules=[
            # PR visual review publishes public before/after screenshots under this path.
            # No credential is sent: GET-only for the report and its image assets, not
            # general access to the S3 host or arbitrary object storage operations.
            EgressPolicySpecRules(
                hosts=["s3.allegedly.works"], methods=[EgressPolicySpecRulesMethods.GET], paths=["/pr-visuals/**"]
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-buildbuddy",
        metadata=ApiObjectMetadata(name=BUILDBUDDY_POLICY, namespace=namespace),
        rules=[
            # app.buildbuddy.io serves the HTTP API (including invocation details and logs);
            # remote.buildbuddy.io serves Build Event Service, Remote Execution and remote cache.
            # The API key itself limits BuildBuddy authority to the account's configured access.
            EgressPolicySpecRules(
                hosts=["app.buildbuddy.io", "remote.buildbuddy.io"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=BUILDBUDDY_POLICY),
            )
        ],
    )
    EgressPolicy(
        scope,
        "egresspolicy-github-clone",
        metadata=ApiObjectMetadata(name=GITHUB_CLONE_POLICY, namespace=namespace),
        rules=[
            # `git clone`/`fetch` over the smart-HTTP protocol: ref discovery (GET) then the
            # pack negotiation and transfer (POST), for a repository addressed with or without
            # the `.git` suffix. Only github.com serves this protocol -- the CDN hosts in
            # `github-downloads` never see it. No credentialRef: this is the anonymous surface
            # any unauthenticated client has for a public repository, so a sandbox holding it
            # gains no identity, only the ability to attempt the same request GitHub already
            # answers for a public repo (or 401s/404s for a private one it has no other access
            # to).
            EgressPolicySpecRules(
                hosts=["github.com"],
                methods=[EgressPolicySpecRulesMethods.GET, EgressPolicySpecRulesMethods.POST],
                paths=["/*/*.git/info/refs", "/*/*.git/git-upload-pack", "/*/*/info/refs", "/*/*/git-upload-pack"],
            )
        ],
    )


class Egress(Construct):
    """The central egress proxy: ServiceAccount, RBAC, interception CA/trust bundle,
    Deployment, Services, CiliumNetworkPolicy, optional PodDisruptionBudget, and the
    EgressCredential/EgressPolicy resources it reads.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env
        self.proxy = proxy(env.namespace)
        self.agent_api = agent_api(env.namespace)
        self.admin = admin(env.namespace)

        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the proxy
        # calls TokenReview as itself, so it needs its own mounted token.
        service_account = ServiceAccount(
            self, "serviceaccount", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
        self._add_rbac(service_account)
        self._add_certificate_and_bundle()
        self.upstream_bundle = self._add_upstream_bundle()
        # Settings this deployment supplies as YAML rather than flags, so a list is a list.
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-settings", namespace=env.namespace),
            model=Settings,
            content={
                "allowed_service_account_namespaces": [env.namespace, *env.egress.external_workload_namespaces],
                "projected_token_audiences": [KUBERNETES_AUDIENCE, notifications.TOKEN_AUDIENCE],
            },
            # A directory of its own: the CA volumes mount under /etc/agentplane-egress, and nothing
            # can mount inside a read-only ConfigMap volume.
            path="/etc/agentplane-egress/settings/settings.yaml",
        )
        deployment = self._add_deployment(service_account, settings)
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
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace),
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
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace),
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
            target_namespaces=(self.env.namespace, *self.env.egress.external_workload_namespaces),
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
        )

    def _add_deployment(self, service_account: ServiceAccount, settings: SettingsFile) -> Deployment:
        ca_secret = Secret.from_secret_name(self, "ca-secret-ref", self.env.egress.ca_secret_name)
        ca_volume = Volume.from_secret(self, "ca-volume", ca_secret, name="ca")
        confdir_volume = Volume.from_empty_dir(self, "confdir-volume", "confdir")
        upstream_ca_volume = Volume.from_config_map(
            self,
            "upstream-ca-volume",
            ConfigMap.from_config_map_name(self, "upstream-ca-ref", self.upstream_bundle.name),
            name="upstream-ca",
        )

        # The managed role's connection URI, which database.py mints.
        database_url = SecretRef(namespace=self.env.namespace, name="postgres-egress").key("uri")
        migrate_env = {
            env_name(MigrationSettings, "database_url"): database_url.env_value(self, "postgres-egress-secret")
        }

        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace, labels=_LABELS),
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
                listen_port=self.proxy.pod_port,
                admin_port=self.admin.pod_port,
                agent_api_port=self.agent_api.pod_port,
                ca_cert="/etc/agentplane-egress/ca/tls.crt",
                ca_key="/etc/agentplane-egress/ca/tls.key",
                confdir="/var/lib/agentplane-egress",
                upstream_ca_file=f"{_UPSTREAM_CA_DIR}/{CA_BUNDLE_KEY}",
                token_audience=llm_ingress.WORKLOAD_TOKEN_AUDIENCE,
            ),
            env_variables={
                env_name(Settings, "database_url"): database_url.env_value(self, "postgres-egress-secret-proxy")
            },
            ports=[
                self.proxy.port.container_port(),
                self.admin.port.container_port(),
                ContainerPort(name="agent-api", number=self.agent_api.pod_port, protocol=Protocol.TCP),
            ],
            readiness=http_probe("/healthz", port=self.admin.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/livez", port=self.admin.pod_port, initial_delay_seconds=30, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=Size.gibibytes(1)),
            ),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        deployment.containers[0].mount("/etc/agentplane-egress/ca", ca_volume, read_only=True)
        deployment.containers[0].mount(_UPSTREAM_CA_DIR, upstream_ca_volume, read_only=True)
        deployment.containers[0].mount("/var/lib/agentplane-egress", confdir_volume)
        settings.mount_into(deployment.containers[0], env=CONFIG_FILE_ENV)

        pod_policy.place(deployment, node_scheduling.HIL_OVH, tolerate_control_plane=True)
        pod_policy.harden(deployment)
        return deployment

    def _add_services(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=self.proxy.name, namespace=self.env.namespace),
            selector=deployment,
            ports=[
                ServicePort(
                    name=self.agent_api.port.name,
                    port=self.agent_api.port.number,
                    target_port=self.agent_api.pod_port,
                    protocol=Protocol.TCP,
                ),
                self.proxy.port.service_port(),
            ],
        )
        Service(
            self,
            "service-admin",
            metadata=ApiObjectMetadata(name=self.admin.name, namespace=self.env.namespace),
            selector=deployment,
            ports=[self.admin.port.service_port()],
        )

    def _add_pdb(self, min_available: int) -> None:
        add_pod_disruption_budget(
            self, "pdb", name=NAME, namespace=self.env.namespace, min_available=min_available, selector=_LABELS
        )

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        (litellm_spec,) = litellm_proxy.proxy_specs()
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=self.proxy.pods.selector,
            ingress=[
                # Runner Pods, and the sandbox Actions' command boxes. app.py and command_sandbox.py
                # import this module, so their Pods are named here.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-runner"),
                    cilium.endpoint_labels(namespace, "agentplane-sandbox"),
                    ports=[self.proxy.pod_port],
                ),
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-app"), ports=[self.admin.pod_port]
                ),
                self.agent_api.pods.admit(self.agent_api.pod_port),
            ],
            egress=[
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                self.agent_api.egress(),
                llm_ingress.service(namespace).egress(),
                actions.service(namespace).egress(),
                notifications.service(namespace).egress(),
                forgejo.HTTP.egress(),
                ollama.AUTH_PROXY.egress(),
                litellm_proxy.service(litellm_spec).egress(),
                # hostNetwork: Cilium sees the node, not an endpoint.
                EgressRule.to_entities(Entity.REMOTE_NODE, Entity.HOST, ports=[home_assistant.SERVICE.port.number]),
                *cilium.open_internet_egress(ports=[443, 80]),
            ],
        )
