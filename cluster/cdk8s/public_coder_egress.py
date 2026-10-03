"""Static OpenClaw's Agentplane attachment. The devbox deliberately stays on Iron.

Only the relay holds the Pod-bound token. Credential values stay in the gateway's
isolated namespace; the app receives canonical placeholders under its existing env names.
"""

from agentplane_egressbinding_crds.works.allegedly.agentplane import EgressBindingSpecSubjects
from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount, k8s
from constructs import Construct

from agentplane.egress import sidecar
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.app_settings import PUBLIC_INTERNET_POLICY
from cluster.cdk8s.agentplane.egress_credentials import EXTERNAL_CREDS_STORE, credential_external_secret
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.haku import console, kube_api_proxy
from cluster.cdk8s.providers.agentplane.egress_binding import EgressBinding
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy
from util.settings_contract import env_name

NAMESPACE = "public-coder-agent"
LABELS = {"app.kubernetes.io/name": NAMESPACE}
SERVICE_ACCOUNT = "openclaw"
GATEWAY = egress.proxy("agentplane-staging")
CA_BUNDLE_NAME = "agentplane-egress-ca"
POLICY = "public-coder-openclaw"
GITHUB_CREDENTIAL = "github-pat"
HAKU_CREDENTIAL = "public-coder-haku-console"
CLICKHOUSE_CREDENTIAL = "public-coder-clickhouse"
AIQUOTA_CREDENTIAL = "aiquota-read"
BRAVE_CREDENTIAL = "brave-search"
MATRIX_CREDENTIAL = "public-coder-matrix"
TOKEN_VOLUME = "agentplane-egress-token"
_TOKEN_DIR = "/var/run/agentplane-egress"
PROXY_URL = "http://127.0.0.1:3128"
SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"


def relay_container() -> k8s.Container:
    return k8s.Container(
        name="egress-sidecar",
        image=f"{SIDECAR_IMAGE}:unset",
        env=[
            k8s.EnvVar(name=env_name(sidecar.Settings, "proxy_host"), value=GATEWAY.fqdn),
            k8s.EnvVar(name=env_name(sidecar.Settings, "proxy_port"), value=str(GATEWAY.pod_port)),
            k8s.EnvVar(name=env_name(sidecar.Settings, "token_file"), value=f"{_TOKEN_DIR}/token"),
        ],
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
                path=sidecar.READINESS_PATH, port=k8s.IntOrString.from_number(sidecar.READINESS_PORT)
            ),
            period_seconds=2,
            timeout_seconds=1,
            failure_threshold=1,
        ),
        volume_mounts=[k8s.VolumeMount(name=TOKEN_VOLUME, mount_path=_TOKEN_DIR, read_only=True)],
    )


def token_volume() -> k8s.Volume:
    return k8s.Volume(
        name=TOKEN_VOLUME,
        projected=k8s.ProjectedVolumeSource(
            default_mode=0o440,
            sources=[
                k8s.VolumeProjection(
                    service_account_token=k8s.ServiceAccountTokenProjection(
                        audience="agentplane-egress", expiration_seconds=600, path="token"
                    )
                )
            ],
        ),
    )


def add_gateway_resources(
    scope: Construct, *, namespace: str, credentials_namespace: str, reader: ServiceAccount
) -> None:
    """Copy canonical sources, never the app's Iron mirrors (no app/bootstrap dependency)."""
    scope = Construct(scope, "public-coder-openclaw-egress")
    _copied_credential(
        scope,
        reader=reader,
        namespace=namespace,
        credentials_namespace=credentials_namespace,
        credential=HAKU_CREDENTIAL,
        source_namespace="authentik",
        secret="haku-console-public-coder-agent",
        key="token",
        description="Public coder's existing static-Agent Haku Console bearer, also used by its Kubernetes authorization proxy. No change of Haku identity or approval policy.",
        target=EgressCredentialSpecTargets(
            header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
        ),
    )
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
    EgressPolicy(
        scope,
        "policy",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        rules=[
            EgressPolicySpecRules(
                hosts=["github.com", "api.github.com", "codeload.github.com"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=GITHUB_CREDENTIAL),
            ),
            EgressPolicySpecRules(
                hosts=[console.HOSTNAME, kube_api_proxy.HOSTNAME],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=HAKU_CREDENTIAL),
            ),
            EgressPolicySpecRules(
                hosts=[client.HTTP.fqdn],
                cluster_internal=True,
                credential_ref=EgressPolicySpecRulesCredentialRef(name=CLICKHOUSE_CREDENTIAL),
            ),
            EgressPolicySpecRules(
                hosts=["aiquota.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/v1/quotas", "/v1/providers/*/raw"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=AIQUOTA_CREDENTIAL),
            ),
            EgressPolicySpecRules(
                hosts=["api.search.brave.com"], credential_ref=EgressPolicySpecRulesCredentialRef(name=BRAVE_CREDENTIAL)
            ),
            EgressPolicySpecRules(
                hosts=["matrix.allegedly.works"],
                methods=[EgressPolicySpecRulesMethods.POST],
                paths=["/_matrix/client/v3/login"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name=MATRIX_CREDENTIAL),
            ),
            # Discovery only, not the broader basic policy's LLM/Actions/Kubernetes grants.
            EgressPolicySpecRules(
                hosts=[GATEWAY.fqdn],
                cluster_internal=True,
                methods=[EgressPolicySpecRulesMethods.GET],
                paths=["/v1/rules", "/openapi.json"],
                credential_ref=EgressPolicySpecRulesCredentialRef(name="agentplane-workload"),
            ),
        ],
    )
    EgressBinding(
        scope,
        "binding",
        metadata=ApiObjectMetadata(name=POLICY, namespace=namespace),
        subjects=[EgressBindingSpecSubjects(namespace=NAMESPACE, name=SERVICE_ACCOUNT)],
        policies=[POLICY, PUBLIC_INTERNET_POLICY],
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
