"""agentplane-testing's Dex: Deployment, Service, HTTPRoute, NetworkPolicy, and the
ESO-generated credentials (4 Password generators + 3 ExternalSecrets, one carrying the
inline Dex config.yaml). Testing-only: staging federates directly to the shared Authentik.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    Capability,
    ContainerResources,
    ContainerSecurityContextProps,
    ContainerSecutiryContextCapabilities,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    PodSecurityContextProps,
    Secret,
    Service,
    Volume,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromRewrite,
    ExternalSecretSpecDataFromRewriteRegexp,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s import cilium, pod_policy
from cluster.cdk8s.agentplane import actions, app as app_component
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.minted_secret import password_generator
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy, deny_all_egress
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NAMESPACE = "agentplane-testing"
_NAME = "dex"
# The Dex Pods' `app.kubernetes.io/name` label value, not an object name.
_POD_LABEL = "agentplane-testing-dex"
_IMAGE = "ghcr.io/dexidp/dex:v2.45.1"
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=5556),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _POD_LABEL),)),
)
# The client Secrets this Dex writes, which the app and the Action Service read.
_OIDC = app_component.oidc_secret(_NAMESPACE)
_MCP_OAUTH = actions.mcp_oauth_secret(_NAMESPACE)
_ACCEPTANCE_OPERATOR = SecretRef(namespace=_NAMESPACE, name="agentplane-testing-acceptance-operator")
_ISSUER = "https://agentplane-dex-testing.allegedly.works/dex"
# Shared between Dex's own staticPasswords entry and the ExternalSecret template the
# acceptance suite reads, so the two can't name different identities.
_ACCEPTANCE_EMAIL = "test-user@agentplane-testing.invalid"
_ACCEPTANCE_USERNAME = "test-user"
_CONFIG_DIR = "/etc/dex"


def service() -> ServiceRef:
    """The testing Dex backend, including its Gateway target port."""
    return _SERVICE


def _dex_config_yaml() -> str:
    return yaml_config(
        {
            "issuer": _ISSUER,
            "storage": {"type": "memory"},
            "web": {"http": f"0.0.0.0:{_SERVICE.pod_port}"},
            "oauth2": {"skipApprovalScreen": True},
            "enablePasswordDB": True,
            "staticClients": [
                {
                    "id": "agentplane-testing",
                    "name": "Agentplane testing",
                    "secretEnv": "DEX_CLIENT_SECRET",
                    "redirectURIs": ["https://agentplane-testing.allegedly.works/auth/callback"],
                },
                {
                    "id": "agentplane-testing-mcp",
                    "name": "Agentplane MCP testing",
                    "secretEnv": "MCP_CLIENT_SECRET",
                    "redirectURIs": ["https://agentplane-testing.allegedly.works/mcp-linkage/callback"],
                },
            ],
            "staticPasswords": [
                {
                    "email": _ACCEPTANCE_EMAIL,
                    "hash": (
                        f'{{{{ regexReplaceAll "^{_ACCEPTANCE_USERNAME}:" '
                        f'(htpasswd "{_ACCEPTANCE_USERNAME}" .password "bcrypt") "" }}}}'
                    ),
                    "username": _ACCEPTANCE_USERNAME,
                    "preferredUsername": _ACCEPTANCE_USERNAME,
                    "userID": "test-subject",
                }
            ],
        }
    )


def _add_credentials(scope: Construct) -> None:
    operator_password = password_generator(
        scope,
        "dex-operator-password-generator",
        name="agentplane-testing-dex-operator-password",
        namespace=_NAMESPACE,
        length=48,
        digits=12,
    )
    client_secret = password_generator(
        scope,
        "dex-client-secret-generator",
        name="agentplane-testing-dex-client-secret",
        namespace=_NAMESPACE,
        length=48,
        digits=12,
    )
    mcp_client_secret = password_generator(
        scope,
        "mcp-client-secret-generator",
        name="agentplane-testing-mcp-client-secret",
        namespace=_NAMESPACE,
        length=48,
        digits=12,
    )
    session_secret = password_generator(
        scope,
        "session-secret-generator",
        name="agentplane-testing-agentplane-session-secret",
        namespace=_NAMESPACE,
        length=64,
        digits=16,
    )

    def rewrite(source: str, target: str) -> ExternalSecretSpecDataFrom:
        return DataFrom.from_password_generator(
            source,
            rewrite=[
                ExternalSecretSpecDataFromRewrite(
                    regexp=ExternalSecretSpecDataFromRewriteRegexp(source="password", target=target)
                )
            ],
        )

    ExternalSecret(
        scope,
        "oidc-credentials",
        metadata=ApiObjectMetadata(
            name=_OIDC.name,
            namespace=_OIDC.namespace,
            annotations={"description": "ESO-generated Dex client credentials and Agentplane session signing key."},
        ),
        refresh_interval="8760h",
        data_from=[rewrite(client_secret, "client-secret"), rewrite(session_secret, "session-secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            data={
                "client-id": "agentplane-testing",
                "client-secret": '{{ index . "client-secret" }}',
                "session-secret": '{{ index . "session-secret" }}',
            },
        ),
    )

    ExternalSecret(
        scope,
        "mcp-oauth-credentials",
        metadata=ApiObjectMetadata(
            name=_MCP_OAUTH.name,
            namespace=_MCP_OAUTH.namespace,
            annotations={"description": "ESO-generated credentials for the testing MCP client registered in Dex."},
        ),
        refresh_interval="8760h",
        data_from=[rewrite(mcp_client_secret, "client-secret")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            data={"client-id": "agentplane-testing-mcp", "client-secret": '{{ index . "client-secret" }}'},
        ),
    )

    ExternalSecret(
        scope,
        "acceptance-operator-credentials",
        metadata=ApiObjectMetadata(
            name=_ACCEPTANCE_OPERATOR.name,
            namespace=_ACCEPTANCE_OPERATOR.namespace,
            annotations={
                "description": "Generates the acceptance password and Dex config together from one password value."
            },
        ),
        refresh_interval="8760h",
        # Dex's config and the acceptance client's password both come from this one
        # dataFrom entry: two ExternalSecrets naming the same Password generator get two
        # independent values (#7042).
        data_from=[DataFrom.from_password_generator(operator_password)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            type="Opaque",
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                annotations={
                    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "public-coder-agent",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "public-coder-agent",
                }
            ),
            data={
                "login": _ACCEPTANCE_EMAIL,
                "username": _ACCEPTANCE_USERNAME,
                "password": "{{ .password }}",
                "issuer": _ISSUER,
                # Dex v2.45.1 encodes IDTokenSubject{user_id: "test-subject", conn_id: "local"}
                # as unpadded base64url protobuf, not the bare staticPasswords.userID.
                # TODO: derive this from readable user/connector IDs instead of hand-maintaining
                # the encoded subject.
                "subject": "Cgx0ZXN0LXN1YmplY3QSBWxvY2Fs",
                "config.yaml": _dex_config_yaml(),
            },
        ),
    )


def _add_deployment(scope: Construct) -> Deployment:
    deployment = Deployment(
        scope,
        "deployment",
        metadata=ApiObjectMetadata(
            name=_NAME,
            namespace=_NAMESPACE,
            labels=_SERVICE.pods.selector,
            annotations={
                # Dex reads its generated config and client secret only at startup.
                "secret.reloader.stakater.com/reload": ",".join(
                    secret.name for secret in (_ACCEPTANCE_OPERATOR, _OIDC, _MCP_OAUTH)
                )
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_SERVICE.pods.selector),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1001, group=1001),
    )
    deployment.add_container(
        name="dex",
        image=_IMAGE,
        # A specific pinned release, not a floating tag -- avoid re-pulling on every restart.
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        args=["dex", "serve", f"{_CONFIG_DIR}/config.yaml"],
        env_variables={
            "DEX_CLIENT_ID": _OIDC.key("client-id").env_value(scope, "oidc-client-id-ref"),
            "DEX_CLIENT_SECRET": _OIDC.key("client-secret").env_value(scope, "oidc-client-secret-ref"),
            "MCP_CLIENT_SECRET": _MCP_OAUTH.key("client-secret").env_value(scope, "mcp-oauth-client-secret-ref"),
        },
        ports=[_SERVICE.port.container_port()],
        readiness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=0, period_seconds=5),
        liveness=http_probe("/dex/healthz", port=_SERVICE.pod_port, initial_delay_seconds=10, period_seconds=30),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(100)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(128)),
        ),
        security_context=ContainerSecurityContextProps(
            read_only_root_filesystem=True, capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL])
        ),
    )
    config_secret = Secret.from_secret_name(scope, "config-secret", _ACCEPTANCE_OPERATOR.name)
    config_volume = Volume.from_secret(
        scope, "config-volume", config_secret, items={"config.yaml": PathMapping(path="config.yaml")}
    )
    tmp_volume = Volume.from_empty_dir(scope, "tmp-volume", "tmp")
    deployment.containers[0].mount(_CONFIG_DIR, config_volume, read_only=True)
    deployment.containers[0].mount("/tmp", tmp_volume)

    pod_policy.harden(deployment)
    return deployment


def _add_service(scope: Construct, deployment: Deployment) -> None:
    Service(
        scope,
        "service",
        metadata=ApiObjectMetadata(name=_SERVICE.name, namespace=_NAMESPACE),
        selector=deployment,
        ports=[_SERVICE.port.service_port()],
    )


def _add_http_route(scope: Construct) -> None:
    https_route(
        scope,
        "httproute",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=["agentplane-dex-testing.allegedly.works"],
        backend=_SERVICE,
    )


def _add_network_policy(scope: Construct) -> None:
    NetworkPolicy(
        scope,
        "networkpolicy",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        endpoint_selector=_SERVICE.pods.selector,
        ingress=[
            IngressRule.from_gateway(_SERVICE.pod_port),
            app_component.service(_NAMESPACE).pods.admit(_SERVICE.pod_port),
            IngressRule.from_endpoints(
                cilium.endpoint_labels(_NAMESPACE, "agentplane-oauth-fixture"), ports=[_SERVICE.pod_port]
            ),
        ],
        egress=[cilium.dns_egress()],
        egress_deny=deny_all_egress(),
    )


class Dex(Construct):
    """Dex Deployment/Service/HTTPRoute/NetworkPolicy and the ESO-generated
    credentials for agentplane-testing's isolated OIDC provider.
    """

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        _add_credentials(self)
        deployment = _add_deployment(self)
        _add_service(self, deployment)
        _add_http_route(self)
        _add_network_policy(self)
