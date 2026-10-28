"""The central egress proxy, its interception CA/trust bundle, and the
EgressCredential resources and egress PolicySets it reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    Node,
    NodeLabelQuery,
    NodeTaintQuery,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    Service,
    ServiceAccount,
    TaintEffect,
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

from agentplane.crd_group import GROUP
from agentplane.egress.settings import Settings
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import actions, database, llm_ingress, notifications
from cluster.cdk8s.agentplane.egress_credentials import BUILDBUDDY_API_KEY_SECRET, GITHUB_PAT_SECRET
from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.cert_manager.interception_ca import interception_root_ca
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo import app as forgejo  # a bare `app.HTTP` would not say whose
from cluster.cdk8s.home_assistant import app as home_assistant  # a bare `app.SERVICE` would not say whose
from cluster.cdk8s.litellm import proxy as litellm_proxy
from cluster.cdk8s.litellm.credentials import CHEAP_EXPERIMENTS_KEY
from cluster.cdk8s.ollama import app as ollama
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cert_manager.bundle import Bundle
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import checked_value, env_name, validate_settings_fragment

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "egress"
_PROXY_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-proxy"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-history-migrate"
_LABELS = {"app.kubernetes.io/name": NAME}
PROXY_PORT = 8888
# The health service whose verdict is "the process answers", whatever the index says; keep equal to
# `LivenessService` in agentplane/egress/admin/grpc_health.go.
_LIVENESS_HEALTH_SERVICE = "livez"
# The API server's own ServiceAccount token audience (Talos `--api-audiences`, equal to
# `--service-account-issuer`): an issuer ID, not an address; never "fix" it to the Service DNS name.
# The sidecar projects and the proxy reviews this exact string, so a mismatch with the cluster shows
# up as a 401 inside the sandbox, not a proxy denial.
KUBERNETES_AUDIENCE = "https://localhost:7445"
# Only the app's authenticated admin gRPC caller receives this audience.
ADMIN_GRPC_AUDIENCE = "agentplane-egress-admin"
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

# The egress PolicySets created below that every environment ships. A name is exported
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
# What the app grants every new sandbox before the caller's own picks. Platform operations and
# inference experiments are granted independently: the latter is a fleet default, not part of the
# basic Agentplane platform policy.
DEFAULT_POLICIES = (BASIC_POLICY, INFERENCE_EXPERIMENTS_POLICY)


_DEFAULT_MEMORY_LIMIT = Size.gibibytes(1)
_DEFAULT_MIGRATE_MEMORY_LIMIT = Size.mebibytes(256)


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


def add_runner_egress(
    scope: Construct, *, policy: str, credential: str, endpoint: ServiceRef, path: str, audience: str, purpose: str
) -> None:
    """The path a runner dials `endpoint` on: `policy` admits POSTs to `path` through the proxy, which
    swaps the `credential` placeholder for the Pod's own token for `audience`, a token no other
    recipient accepts. `purpose` finishes "minted only for its runner's ..." in the description.

    The fleet chart adds its Cilium policy separately."""
    namespace = endpoint.pods.namespace
    EgressCredential(
        scope,
        f"{policy}-credential",
        metadata=ApiObjectMetadata(name=credential, namespace=namespace),
        description=(
            f"The calling Sandbox Pod's short-lived, Pod-bound identity minted only for its runner's {purpose}. "
            "It conveys no other authority."
        ),
        source=Source.projected_workload_token(audience=audience),
        targets=[
            EgressCredentialSpecTargets(
                header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
            )
        ],
    )
    add_egress_policy(
        scope,
        f"{policy}-policy",
        metadata=ApiObjectMetadata(name=policy, namespace=namespace),
        rules=[
            Rule.egress(
                hosts=[endpoint.fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.POST],
                paths=[path],
                credential_ref=PolicySetSpecRulesCredentialRef(name=credential),
            )
        ],
    )


def add_runner_egress_network_policy(scope: Construct, *, policy: str, endpoint: ServiceRef) -> None:
    """The fleet proxy fence is additive to the proxy's own network policy."""
    namespace = endpoint.pods.namespace
    NetworkPolicy(
        scope,
        f"{policy}-proxy-egress",
        metadata=ApiObjectMetadata(
            name=f"{endpoint.name.removeprefix('agentplane-')}-{policy}-proxy", namespace=namespace
        ),
        endpoint_selector=proxy(namespace).pods.selector,
        egress=[endpoint.egress()],
    )


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


def _egress_policies(scope: Construct, *, namespace: str, litellm_host: str, litellm_cluster_internal: bool) -> None:
    add_egress_policy(
        scope,
        "egresspolicy-inference-experiments",
        metadata=ApiObjectMetadata(name=INFERENCE_EXPERIMENTS_POLICY, namespace=namespace),
        rules=[
            Rule.egress(
                hosts=[ollama.AUTH_PROXY.fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/api/tags", "/api/ps", "/api/version", "/v1/models"],
                credential_ref=PolicySetSpecRulesCredentialRef(name="ollama"),
            ),
            Rule.egress(
                hosts=[ollama.AUTH_PROXY.fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.POST],
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
                credential_ref=PolicySetSpecRulesCredentialRef(name="ollama"),
            ),
            Rule.egress(
                hosts=[litellm_host],
                cluster_internal=litellm_cluster_internal,
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/v1/models", "/models", "/model/info"],
                credential_ref=PolicySetSpecRulesCredentialRef(name="litellm-cheap-experiments"),
            ),
            Rule.egress(
                hosts=[litellm_host],
                cluster_internal=litellm_cluster_internal,
                methods=[PolicySetSpecRulesMethods.POST],
                paths=[
                    "/v1/chat/completions",
                    "/v1/completions",
                    "/v1/responses",
                    "/v1/messages",
                    "/v1/messages/count_tokens",
                    "/v1/embeddings",
                    "/v1/audio/transcriptions",
                ],
                credential_ref=PolicySetSpecRulesCredentialRef(name="litellm-cheap-experiments"),
            ),
        ],
    )
    # Available for explicit grants only: no defaults, presets or standing bindings opt in.
    add_egress_policy(
        scope,
        "public-internet-policy",
        metadata=ApiObjectMetadata(name=PUBLIC_INTERNET_POLICY, namespace=namespace),
        rules=[Rule.egress(hosts=["*"])],
    )
    add_egress_policy(
        scope,
        "egresspolicy-basic",
        metadata=ApiObjectMetadata(name=BASIC_POLICY, namespace=namespace),
        rules=[
            # The proxy matches a request's host on the exact string, so the in-cluster hosts are the
            # full names their clients spell.
            #
            # Whole services, as for the API server below: each authenticates the workload token the
            # proxy substitutes and authorizes the caller itself, so a method or path list here would
            # only be a second copy of their routes to keep in step.
            Rule.egress(
                hosts=[llm_ingress.service(namespace).fqdn, actions.service(namespace).fqdn],
                cluster_internal=True,
                credential_ref=PolicySetSpecRulesCredentialRef(name="agentplane-workload"),
            ),
            Rule.egress(
                hosts=[notifications.service(namespace).fqdn],
                cluster_internal=True,
                credential_ref=PolicySetSpecRulesCredentialRef(name=notifications.WORKLOAD_CREDENTIAL),
            ),
            # Narrowed, unlike the services above: this host is also the proxy's own listener, and a
            # rule matches hosts, not ports.
            Rule.egress(
                hosts=[agent_api(namespace).fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/llms.txt", "/docs/agents/**", "/openapi.json", "/v1/rules"],
                credential_ref=PolicySetSpecRulesCredentialRef(name="agentplane-workload"),
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
            Rule.egress(
                hosts=[KUBERNETES_HOST],
                cluster_internal=True,
                credential_ref=PolicySetSpecRulesCredentialRef(name=KUBERNETES_CREDENTIAL),
            ),
        ],
    )
    add_egress_policy(
        scope,
        "egresspolicy-packages",
        metadata=ApiObjectMetadata(name=PACKAGES_POLICY, namespace=namespace),
        rules=[
            # The package and toolchain mirrors a box needs to install anything: without them
            # `pip install`, `npm install`, a cargo fetch, a Go module fetch and every Bazel download fail in a
            # sandbox whose whole purpose is running commands.
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
            Rule.egress(
                hosts=[
                    # keep-sorted start
                    "bcr.bazel.build",
                    "cache.nixos.org",
                    "channels.nixos.org",
                    "code.forgejo.org",
                    "data.forgejo.org",
                    "dl.google.com",
                    "files.pythonhosted.org",
                    "ftp.gnu.org",
                    "ghcr.io",
                    "go.dev",
                    "index.crates.io",
                    "nixos.org",
                    "nodejs.org",
                    # ghcr.io redirects blob reads here, so a pull fails without it. A
                    # githubusercontent host in this policy rather than the GitHub one because
                    # it carries container layers, not repository content, and needs no token.
                    "pkg-containers.githubusercontent.com",
                    "proxy.golang.org",
                    "pypi.org",
                    "registry.npmjs.org",
                    "releases.bazel.build",
                    "snapshot.debian.org",
                    "static.crates.io",
                    "static.rust-lang.org",
                    # keep-sorted end
                ],
                methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.HEAD],
            )
        ],
    )
    add_egress_policy(
        scope,
        "egresspolicy-github-agentydragon-agent",
        metadata=ApiObjectMetadata(name=GITHUB_AGENTYDRAGON_AGENT_POLICY, namespace=namespace),
        rules=[
            # The bot across GitHub's REST API and git protocol, including the verbs that let it
            # finish a change it already opened: PATCH on a pull request's body, PUT on
            # `update-branch` and `merge`, DELETE on a review comment or a head branch. GET and POST
            # alone leave a sandbox that can file a PR and cannot correct one; the first time it
            # mattered it worked around the refusal by commenting beside a stale description instead
            # of editing it, which costs the reviewer more than the edit (#10625).
            #
            # DELETE deserves a look, because it is the verb that destroys. It is here deliberately:
            # an unpathed rule that substitutes a write-capable PAT does not bound what the bot may
            # do, only how it asks, so a verb list narrower than the Secret behind it is a second,
            # weaker copy of that boundary rather than the boundary. A real limit here would be
            # paths, which this rule has never carried; if one is wanted, add paths, do not trim
            # verbs.
            Rule.egress(
                hosts=["api.github.com", "github.com", "codeload.github.com", "*.githubusercontent.com"],
                methods=[
                    PolicySetSpecRulesMethods.GET,
                    PolicySetSpecRulesMethods.POST,
                    PolicySetSpecRulesMethods.PUT,
                    PolicySetSpecRulesMethods.PATCH,
                    PolicySetSpecRulesMethods.DELETE,
                ],
                credential_ref=PolicySetSpecRulesCredentialRef(name="github-pat"),
            )
        ],
    )
    add_egress_policy(
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
            Rule.egress(hosts=["*.blob.core.windows.net"], methods=[PolicySetSpecRulesMethods.GET])
        ],
    )
    add_egress_policy(
        scope,
        "egresspolicy-public-coder-pr-visuals",
        metadata=ApiObjectMetadata(name=PUBLIC_CODER_VISUALS_POLICY, namespace=namespace),
        rules=[
            # PR visual review publishes public before/after screenshots under this path.
            # No credential is sent: GET-only for the report and its image assets, not
            # general access to the S3 host or arbitrary object storage operations.
            Rule.egress(hosts=["s3.allegedly.works"], methods=[PolicySetSpecRulesMethods.GET], paths=["/pr-visuals/**"])
        ],
    )
    add_egress_policy(
        scope,
        "egresspolicy-buildbuddy",
        metadata=ApiObjectMetadata(name=BUILDBUDDY_POLICY, namespace=namespace),
        rules=[
            # app.buildbuddy.io serves the HTTP API (including invocation details and logs);
            # remote.buildbuddy.io serves Build Event Service, Remote Execution and remote cache.
            # The API key itself limits BuildBuddy authority to the account's configured access.
            Rule.egress(
                hosts=["app.buildbuddy.io", "remote.buildbuddy.io"],
                credential_ref=PolicySetSpecRulesCredentialRef(name=BUILDBUDDY_POLICY),
            )
        ],
    )
    add_egress_policy(
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
            Rule.egress(
                hosts=["github.com"],
                methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.POST],
                paths=["/*/*.git/info/refs", "/*/*.git/git-upload-pack", "/*/*/info/refs", "/*/*/git-upload-pack"],
            )
        ],
    )


class Egress(Construct):
    """The central egress proxy: ServiceAccount, RBAC, Deployment, Services,
    optional PodDisruptionBudget, and the EgressCredential and PolicySet resources it
    reads. Fleet CA issuance, trust bundles, and Cilium policy are added separately.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        runners: Pods,
        admin_grpc_callers: Sequence[ServiceAccountRef],
        agent_api_service: ServiceRef | None = None,
        litellm_host: str | None = None,
        litellm_cluster_internal: bool = True,
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        container_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_memory_limit: Size | None = _DEFAULT_MIGRATE_MEMORY_LIMIT,
        migrate_env: Mapping[str, EnvValue] | None = None,
    ) -> None:
        """`runners` are the runner Pods, which the proxy admits."""
        super().__init__(scope, id)
        self.env = env
        self.database_url = database_url
        self.proxy = proxy(env.namespace)
        self.agent_api = agent_api_service or agent_api(env.namespace)
        self.admin = admin(env.namespace)
        self.runners = runners
        self.placement = placement
        self.registry_secret_name = registry_secret_name
        self.container_env = container_env or {}
        self.memory_limit = memory_limit
        self.migrate_pull_policy = migrate_pull_policy
        self.migrate_memory_limit = migrate_memory_limit
        self.migrate_env = migrate_env or {}

        # Settings this deployment supplies as YAML rather than flags, so a list is a list.
        content = {
            "allowed_service_account_namespaces": [env.namespace, *env.egress.external_workload_namespaces],
            "admin_grpc_audience": ADMIN_GRPC_AUDIENCE,
            "admin_grpc_callers": [
                f"{account.namespace}/{account.name}"
                for account in sorted(admin_grpc_callers, key=lambda account: (account.namespace, account.name))
            ],
            "projected_token_audiences": [KUBERNETES_AUDIENCE, notifications.TOKEN_AUDIENCE, RUNNER_INGRESS_AUDIENCE],
        }
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name=f"{NAME}-go-settings", namespace=env.namespace),
            content=yaml_config(validate_settings_fragment(Settings, content)),
            # A directory of its own: the CA volumes mount under /etc/agentplane-egress, and nothing
            # can mount inside a read-only ConfigMap volume.
            path="/etc/agentplane-egress/settings/settings.yaml",
        )
        workload = self._add_workload(settings)
        self._add_rbac(workload.account)
        Service(
            self,
            "service-admin",
            metadata=ApiObjectMetadata(name=self.admin.name, namespace=env.namespace),
            selector=workload.deployment,
            ports=[self.admin.port.service_port()],
        )
        _egress_credentials(self, namespace=env.namespace)
        (litellm_spec,) = litellm_proxy.proxy_specs()
        _egress_policies(
            self,
            namespace=env.namespace,
            litellm_host=litellm_host or litellm_proxy.service(litellm_spec).fqdn,
            litellm_cluster_internal=litellm_cluster_internal,
        )

    def add_platform_resources(self) -> None:
        """Add fleet CA issuance, trust bundle, and Cilium network admission."""
        self._add_certificate_and_bundle()
        self._add_upstream_bundle()
        self._add_network_policy(self.runners)

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
        # What the proxy reads to decide a request: policy sets, bindings, credentials.
        # No Pods (TokenReview already names the subject) and no Secrets (an
        # EgressCredential names one, but the values live in a separate namespace
        # this Role doesn't grant). Its one write is its own EgressReady condition.
        Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=self.env.namespace),
            rules=[
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource)
                        for resource in ["egresscredentials", "policysets", "policybindings"]
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(resources=[custom_resource(GROUP, "policybindings/status")], verbs=["patch"]),
            ],
        )
        RoleBinding(
            self,
            "rolebinding",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=self.env.namespace),
            role=Role.from_role_name(self, "role-ref", NAME.removeprefix("agentplane-")),
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

    def _add_workload(self, settings: SettingsFile) -> ServiceWorkload:
        ca_secret = Secret.from_secret_name(self, "ca-secret-ref", self.env.egress.ca_secret_name)
        ca_volume = Volume.from_secret(self, "ca-volume", ca_secret, name="ca")
        upstream_ca_volume = Volume.from_config_map(
            self,
            "upstream-ca-volume",
            ConfigMap.from_config_map_name(self, "upstream-ca-ref", f"{self.env.namespace}-egress-upstream-ca"),
            name="upstream-ca",
        )

        values: dict[str, object] = {
            "rules_namespace": self.env.namespace,
            "credentials_namespace": self.env.egress.credentials_namespace,
            "listen_address": f"0.0.0.0:{self.proxy.pod_port}",
            "admin_listen_address": f"0.0.0.0:{self.admin.pod_port}",
            "agent_api_listen_address": f"0.0.0.0:{self.agent_api.pod_port}",
            "ca_cert": "/etc/agentplane-egress/ca/tls.crt",
            "ca_key": "/etc/agentplane-egress/ca/tls.key",
            "upstream_ca_file": f"{_UPSTREAM_CA_DIR}/{CA_BUNDLE_KEY}",
            "token_audience": WORKLOAD_TOKEN_AUDIENCE,
        }
        setting_env = {
            env_name(Settings, field): EnvValue.from_value(str(checked_value(Settings, field, value)))
            for field, value in values.items()
        }

        zones = self.env.egress.zones
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.agent_api, self.proxy],
            container="proxy",
            image=f"{_PROXY_IMAGE}:{_PLACEHOLDER_TAG}",
            env_variables={**setting_env, env_name(Settings, "database_url"): self.database_url, **self.container_env},
            extra_ports=[self.admin.container_port()],
            readiness=grpc_probe(port=self.admin.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(
                port=self.admin.pod_port, service=_LIVENESS_HEALTH_SERVICE, initial_delay_seconds=30, period_seconds=30
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(256), limit=self.memory_limit),
            ),
            termination_grace=Duration.seconds(60),
            migrate=migrate_init_container(
                f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}",
                name="migrate",
                env_variables={"DATABASE_URL": self.database_url, **self.migrate_env},
                image_pull_policy=self.migrate_pull_policy,
                memory_limit=self.migrate_memory_limit,
            ),
            tolerate_control_plane=True,
            placement=self.placement if zones is None else None,
            replica_profile=self.env.egress.replicas,
            traffic_distribution=self.env.egress.traffic_distribution,
            telemetry_enabled=True,
            registry_secret_name=self.registry_secret_name,
        )
        if zones is not None:
            workload.deployment.scheduling.attract(
                Node.labeled(NodeLabelQuery.in_(node_scheduling.ZONE_LABEL, list(zones)))
            )
            workload.deployment.scheduling.tolerate(
                Node.tainted(
                    NodeTaintQuery.exists(node_scheduling.CONTROL_PLANE_TAINT_KEY, effect=TaintEffect.NO_SCHEDULE)
                )
            )
            # The fluent scheduling API has no topologySpreadConstraints equivalent.
            ApiObject.of(workload.deployment).add_json_patch(
                JsonPatch.add(
                    "/spec/template/spec/topologySpreadConstraints", [node_scheduling.zone_spread(zones, _LABELS)]
                )
            )
        workload.container.mount("/etc/agentplane-egress/ca", ca_volume, read_only=True)
        workload.container.mount(_UPSTREAM_CA_DIR, upstream_ca_volume, read_only=True)
        settings.mount_into(workload.container, env=Settings.config_file_env)
        return workload

    def _add_network_policy(self, runners: Pods) -> None:
        namespace = self.env.namespace
        (litellm_spec,) = litellm_proxy.proxy_specs()
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=namespace),
            endpoint_selector=self.proxy.pods.selector,
            ingress=[
                # Runner Pods, and the sandbox Actions' command boxes. command_sandbox.py imports this
                # module, so its Pods are named here.
                IngressRule.from_endpoints(
                    runners.cilium, cilium.endpoint_labels(namespace, "sandbox"), ports=[self.proxy.pod_port]
                ),
                IngressRule.from_endpoints(cilium.endpoint_labels(namespace, "app"), ports=[self.admin.pod_port]),
                self.agent_api.pods.admit(self.agent_api.pod_port),
            ],
            egress=[
                database.egress(namespace),
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
                # Local Gateway requests also need backend identity/target-port grants: keep those
                # with the public-origin egress PolicySets in egress_staging_credentials.py (#9495).
                *cilium.open_internet_egress(ports=[443, 80]),
            ],
        )
