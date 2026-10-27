"""Static OpenClaw's Agentplane egress attachment.

Only the relay holds the Pod-bound token. Credential values stay in the gateway's
isolated namespace; the app receives canonical placeholders under its existing env names.
"""

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_policybinding_crds.works.allegedly.agentplane import PolicyBindingSpecSubject
from agentplane_policyset_crds.works.allegedly.agentplane import (
    PolicySetSpecRulesCredentialRef,
    PolicySetSpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount, k8s
from constructs import Construct

from agentplane.egress import sidecar_config
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import actions, egress
from cluster.cdk8s.agentplane.egress_credentials import EXTERNAL_CREDS_STORE, credential_external_secret
from cluster.cdk8s.agentplane.egress_policies import add_egress_policy
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.policy_binding import PolicyBinding
from cluster.cdk8s.providers.agentplane.policy_set import Rule
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from cluster.cdk8s.settings_file import PodWiring, SettingsFile
from util.settings_contract import validate_settings_fragment

NAMESPACE = "public-coder-agent"
LABELS = {"app.kubernetes.io/name": NAMESPACE}
SERVICE_ACCOUNT = "openclaw"
GATEWAY = egress.proxy("agentplane-staging")
CA_BUNDLE_NAME = "agentplane-egress-ca"
POLICY = "public-coder-openclaw"
GITHUB_CREDENTIAL = "github-pat"
CLICKHOUSE_CREDENTIAL = "public-coder-clickhouse"
AIQUOTA_CREDENTIAL = "aiquota-read"
BRAVE_CREDENTIAL = "brave-search"
MATRIX_CREDENTIAL = "public-coder-matrix"
TOKEN_VOLUME = "agentplane-egress-token"
_TOKEN_DIR = "/var/run/agentplane-egress"
SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
_SETTINGS_CONFIG_MAP = "openclaw-egress-sidecar"
_SETTINGS_VOLUME = "egress-sidecar-settings"
_SETTINGS_PATH = "/etc/agentplane-egress-sidecar/settings.yaml"


def add_relay_settings(scope: Construct) -> PodWiring:
    """The relay's settings ConfigMap, and what the Pod needs to mount it."""
    content = {"proxy_host": GATEWAY.fqdn, "proxy_port": GATEWAY.pod_port, "token_file": f"{_TOKEN_DIR}/token"}
    validate_settings_fragment(sidecar_config.Settings, content)
    return SettingsFile(
        scope,
        "egress-sidecar-settings",
        metadata=ApiObjectMetadata(name=_SETTINGS_CONFIG_MAP, namespace=NAMESPACE),
        content=yaml_config(content),
        path=_SETTINGS_PATH,
    ).pod_wiring(volume_name=_SETTINGS_VOLUME, env=sidecar_config.Settings.config_file_env)


def relay_container(settings: PodWiring) -> k8s.Container:
    return k8s.Container(
        name="egress-sidecar",
        image=f"{SIDECAR_IMAGE}:unset",
        env=[settings.path_env],
        security_context=k8s.SecurityContext(
            run_as_non_root=True,
            run_as_user=1000,
            allow_privilege_escalation=False,
            capabilities=k8s.Capabilities(drop=["ALL"]),
        ),
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("10m"), "memory": k8s.Quantity.from_string("32Mi")},
            limits={"memory": k8s.Quantity.from_string("128Mi")},
        ),
        readiness_probe=k8s.Probe(
            http_get=k8s.HttpGetAction(
                path=sidecar_config.READINESS_PATH, port=k8s.IntOrString.from_number(sidecar_config.READINESS_PORT)
            ),
            period_seconds=2,
            timeout_seconds=1,
            failure_threshold=1,
        ),
        volume_mounts=[k8s.VolumeMount(name=TOKEN_VOLUME, mount_path=_TOKEN_DIR, read_only=True), settings.mount],
    )


def token_volume() -> k8s.Volume:
    return k8s.Volume(
        name=TOKEN_VOLUME,
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[
                k8s.VolumeProjection(
                    service_account_token=k8s.ServiceAccountTokenProjection(
                        audience=WORKLOAD_TOKEN_AUDIENCE, expiration_seconds=600, path="token"
                    )
                )
            ],
        ),
    )


def add_gateway_resources(
    scope: Construct, *, namespace: str, credentials_namespace: str, reader: ServiceAccount
) -> None:
    """Copy canonical credentials into Agentplane's isolated store through exact-secret stores."""
    scope = Construct(scope, "public-coder-openclaw-egress")
    _copied_credential(
        scope,
        reader=reader,
        namespace=namespace,
        credentials_namespace=credentials_namespace,
        credential=CLICKHOUSE_CREDENTIAL,
        source_namespace=client.NAMESPACE,
        secret=client.PUBLIC_CODER_CREDENTIALS,
        key=client.PASSWORD_KEY,
        description=f"The existing read-only ClickHouse {client.PUBLIC_CODER_USER} password. Present as the Basic password with that username.",
        target=EgressCredentialSpecTargets(
            header="Authorization", method=EgressCredentialSpecTargetsMethod.BASIC_PASSWORD
        ),
    )
    _copied_credential(
        scope,
        reader=reader,
        namespace=namespace,
        credentials_namespace=credentials_namespace,
        credential=MATRIX_CREDENTIAL,
        source_namespace="matrix",
        secret="public-coder-agent-matrix-bot-password",
        key="password",
        description="The existing public-coder Matrix bot login password, only in the exact login password field. Login responses can contain session tokens; this does not hide those from the client.",
        target=EgressCredentialSpecTargets(method=EgressCredentialSpecTargetsMethod.JSON_FIELD, field="password"),
    )
    credential_external_secret(
        scope,
        namespace=credentials_namespace,
        target=BRAVE_CREDENTIAL,
        source="brave-search-api-key",
        key="api-key",
        store=EXTERNAL_CREDS_STORE,
    )
    EgressCredential(
        scope,
        f"credential-{BRAVE_CREDENTIAL}",
        metadata=ApiObjectMetadata(name=BRAVE_CREDENTIAL, namespace=namespace),
        description="The shared Brave Search API key, substituted only at Brave's API in X-Subscription-Token.",
        source=Source.secret_ref(name=BRAVE_CREDENTIAL, key="api-key"),
        targets=[
            EgressCredentialSpecTargets(
                header="X-Subscription-Token", method=EgressCredentialSpecTargetsMethod.WHOLE_VALUE
            )
        ],
    )
    add_egress_policy(
        scope,
        "policy",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        rules=[
            Rule.egress(
                hosts=["github.com", "api.github.com", "codeload.github.com"],
                credential_ref=PolicySetSpecRulesCredentialRef(name=GITHUB_CREDENTIAL),
            ),
            Rule.egress(
                hosts=[client.HTTP.fqdn],
                cluster_internal=True,
                credential_ref=PolicySetSpecRulesCredentialRef(name=CLICKHOUSE_CREDENTIAL),
            ),
            Rule.egress(
                hosts=["aiquota.allegedly.works"],
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/v1/quotas", "/v1/providers/*/raw"],
                credential_ref=PolicySetSpecRulesCredentialRef(name=AIQUOTA_CREDENTIAL),
            ),
            Rule.egress(
                hosts=["api.search.brave.com"], credential_ref=PolicySetSpecRulesCredentialRef(name=BRAVE_CREDENTIAL)
            ),
            Rule.egress(
                hosts=["matrix.allegedly.works"],
                methods=[PolicySetSpecRulesMethods.POST],
                paths=["/_matrix/client/v3/login"],
                credential_ref=PolicySetSpecRulesCredentialRef(name=MATRIX_CREDENTIAL),
            ),
            # Discovery only, not the broader basic policy's LLM/Actions/Kubernetes grants.
            Rule.egress(
                hosts=[GATEWAY.fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET],
                paths=["/v1/rules", "/openapi.json"],
                credential_ref=PolicySetSpecRulesCredentialRef(name="agentplane-workload"),
            ),
            Rule.egress(
                hosts=[actions.service("agentplane-staging").fqdn],
                cluster_internal=True,
                methods=[PolicySetSpecRulesMethods.GET, PolicySetSpecRulesMethods.POST],
                paths=[
                    "/mcp",
                    "/openapi.json",
                    "/v1/action-groups",
                    "/v1/action-groups/**",
                    "/v1/action-policy",
                    "/v1/action-requests",
                    "/v1/action-requests/**",
                ],
                credential_ref=PolicySetSpecRulesCredentialRef(name="agentplane-workload"),
            ),
        ],
    )
    PolicyBinding(
        scope,
        "binding",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        subject=PolicyBindingSpecSubject(namespace=NAMESPACE, name=SERVICE_ACCOUNT),
        policy_sets=[POLICY, egress.INFERENCE_EXPERIMENTS_POLICY, egress.PUBLIC_INTERNET_POLICY],
    )
    NetworkPolicy(
        scope,
        "gateway-network",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        endpoint_selector=GATEWAY.pods.selector,
        ingress=[
            IngressRule.from_endpoints(
                {
                    "k8s:io.kubernetes.pod.namespace": NAMESPACE,
                    **LABELS,
                    "io.cilium.k8s.policy.serviceaccount": SERVICE_ACCOUNT,
                },
                ports=[GATEWAY.pod_port],
            )
        ],
        egress=[client.HTTP.egress()],
    )
    NetworkPolicy(
        scope,
        "clickhouse-ingress",
        metadata=ApiObjectMetadata(name=POLICY, namespace=client.NAMESPACE),
        endpoint_selector=client.LABELS,
        ingress=[
            IngressRule.from_endpoints(cilium.endpoint_labels(namespace, egress.NAME), ports=[client.HTTP.pod_port])
        ],
    )


def _copied_credential(
    scope: Construct,
    *,
    reader: ServiceAccount,
    namespace: str,
    credentials_namespace: str,
    credential: str,
    source_namespace: str,
    secret: str,
    key: str,
    description: str,
    target: EgressCredentialSpecTargets,
) -> None:
    store = single_secret_store(
        scope,
        f"agentplane-{credential}",
        reader=reader,
        source_namespace=source_namespace,
        source_secret=secret,
        consumer_namespace=credentials_namespace,
    )
    credential_external_secret(
        scope, namespace=credentials_namespace, target=credential, source=secret, key=key, store=store
    )
    EgressCredential(
        scope,
        f"credential-{credential}",
        metadata=ApiObjectMetadata(name=credential, namespace=namespace),
        description=description,
        source=Source.secret_ref(name=credential, key=key),
        targets=[target],
    )
