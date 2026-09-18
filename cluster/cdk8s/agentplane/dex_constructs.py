"""agentplane-testing's Dex: Deployment, Service, HTTPRoute, NetworkPolicy, and the
ESO-generated credentials (4 Password generators + 3 ExternalSecrets, one carrying the
inline Dex config.yaml). Testing-only: staging federates directly to the shared Authentik.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Size
from cdk8s_plus_34 import (
    Capability,
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
    PathMapping,
    PodSecurityContextProps,
    Protocol,
    Secret,
    SecretValue,
    Service,
    ServicePort,
    Volume,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromRewrite,
    ExternalSecretSpecDataFromRewriteRegexp,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.agentplane import cilium_helpers
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe

_NAMESPACE = "agentplane-testing"
_NAME = "agentplane-testing-dex"
_IMAGE = "ghcr.io/dexidp/dex:v2.45.1"
_PORT = 5556
_LABELS = {"app.kubernetes.io/name": _NAME}
_ISSUER = "https://agentplane-dex-testing.allegedly.works/dex"
# Shared between Dex's own staticPasswords entry and the ExternalSecret template the
# acceptance suite reads, so the two can't name different identities.
_ACCEPTANCE_EMAIL = "test-user@agentplane-testing.invalid"
_ACCEPTANCE_USERNAME = "test-user"
_CONFIG_DIR = "/etc/dex"


def _password_generator(scope: Construct, id: str, *, name: str, length: int, digits: int) -> None:
    Password(
        scope,
        id,
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE),
        spec=PasswordSpec(length=length, digits=digits, symbols=0, no_upper=False, allow_repeat=True),
    )


def _dex_config_yaml() -> str:
    return yaml_config(
        {
            "issuer": _ISSUER,
            "storage": {"type": "memory"},
            "web": {"http": f"0.0.0.0:{_PORT}"},
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
    _password_generator(
        scope, "dex-operator-password-generator", name="agentplane-testing-dex-operator-password", length=48, digits=12
    )
    _password_generator(
        scope, "dex-client-secret-generator", name="agentplane-testing-dex-client-secret", length=48, digits=12
    )
    _password_generator(
        scope, "mcp-client-secret-generator", name="agentplane-testing-mcp-client-secret", length=48, digits=12
    )
    _password_generator(
        scope, "session-secret-generator", name="agentplane-testing-agentplane-session-secret", length=64, digits=16
    )

    def rewrite(source: str, target: str) -> ExternalSecretSpecDataFrom:
        return ExternalSecretSpecDataFrom(
            source_ref=ExternalSecretSpecDataFromSourceRef(
                generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                    kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD, name=source
                )
            ),
            rewrite=[
                ExternalSecretSpecDataFromRewrite(
                    regexp=ExternalSecretSpecDataFromRewriteRegexp(source="password", target=target)
                )
            ],
        )

    ExternalSecret(
        scope,
        "oidc-credentials",
        metadata=metadata(
            "agentplane-oidc",
            _NAMESPACE,
            annotations={"description": "ESO-generated Dex client credentials and Agentplane session signing key."},
        ),
        spec=ExternalSecretSpec(
            refresh_interval="8760h",
            target=ExternalSecretSpecTarget(
                name="agentplane-oidc",
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
            ),
            data_from=[
                rewrite("agentplane-testing-dex-client-secret", "client-secret"),
                rewrite("agentplane-testing-agentplane-session-secret", "session-secret"),
            ],
        ),
    )

    ExternalSecret(
        scope,
        "mcp-oauth-credentials",
        metadata=metadata(
            "agentplane-mcp-oauth",
            _NAMESPACE,
            annotations={"description": "ESO-generated credentials for the testing MCP client registered in Dex."},
        ),
        spec=ExternalSecretSpec(
            refresh_interval="8760h",
            target=ExternalSecretSpecTarget(
                name="agentplane-mcp-oauth",
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(
                    type="Opaque",
                    data={"client-id": "agentplane-testing-mcp", "client-secret": '{{ index . "client-secret" }}'},
                ),
            ),
            data_from=[rewrite("agentplane-testing-mcp-client-secret", "client-secret")],
        ),
    )

    ExternalSecret(
        scope,
        "acceptance-operator-credentials",
        metadata=metadata(
            "agentplane-testing-acceptance-operator",
            _NAMESPACE,
            annotations={
                "description": "Generates the acceptance password and Dex config together from one password value."
            },
        ),
        spec=ExternalSecretSpec(
            refresh_interval="8760h",
            target=ExternalSecretSpecTarget(
                name="agentplane-testing-acceptance-operator",
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
            ),
            # Dex's config and the acceptance client's password both come from this one
            # dataFrom entry: two ExternalSecrets naming the same Password generator get two
            # independent values (#7042).
            data_from=[
                ExternalSecretSpecDataFrom(
                    source_ref=ExternalSecretSpecDataFromSourceRef(
                        generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                            kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD,
                            name="agentplane-testing-dex-operator-password",
                        )
                    )
                )
            ],
        ),
    )


def _add_deployment(scope: Construct) -> Deployment:
    deployment = Deployment(
        scope,
        "deployment",
        metadata=metadata(
            _NAME,
            _NAMESPACE,
            labels=_LABELS,
            annotations={
                # Dex reads its generated config and client secret only at startup.
                "secret.reloader.stakater.com/reload": "agentplane-testing-acceptance-operator,agentplane-oidc,agentplane-mcp-oauth"
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1001, group=1001),
    )
    oidc_secret = Secret.from_secret_name(scope, "oidc-secret", "agentplane-oidc")
    mcp_oauth_secret = Secret.from_secret_name(scope, "mcp-oauth-secret", "agentplane-mcp-oauth")
    deployment.add_container(
        name="dex",
        image=_IMAGE,
        # A specific pinned release, not a floating tag -- avoid re-pulling on every restart.
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        args=["dex", "serve", f"{_CONFIG_DIR}/config.yaml"],
        env_variables={
            "DEX_CLIENT_ID": EnvValue.from_secret_value(SecretValue(secret=oidc_secret, key="client-id")),
            "DEX_CLIENT_SECRET": EnvValue.from_secret_value(SecretValue(secret=oidc_secret, key="client-secret")),
            "MCP_CLIENT_SECRET": EnvValue.from_secret_value(SecretValue(secret=mcp_oauth_secret, key="client-secret")),
        },
        ports=[ContainerPort(name="http", number=_PORT, protocol=Protocol.TCP)],
        readiness=http_probe("/dex/healthz", port=_PORT, initial_delay_seconds=0, period_seconds=5),
        liveness=http_probe("/dex/healthz", port=_PORT, initial_delay_seconds=10, period_seconds=30),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(100)),
            memory=MemoryResources(request=Size.mebibytes(64), limit=Size.mebibytes(128)),
        ),
        security_context=ContainerSecurityContextProps(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
        ),
    )
    config_secret = Secret.from_secret_name(scope, "config-secret", "agentplane-testing-acceptance-operator")
    config_volume = Volume.from_secret(
        scope, "config-volume", config_secret, items={"config.yaml": PathMapping(path="config.yaml")}
    )
    tmp_volume = Volume.from_empty_dir(scope, "tmp-volume", "tmp")
    deployment.containers[0].mount(_CONFIG_DIR, config_volume, read_only=True)
    deployment.containers[0].mount("/tmp", tmp_volume)

    apply_pod_spec_patches(deployment, labels=_LABELS, topology_spread=False)
    return deployment


def _add_service(scope: Construct, deployment: Deployment) -> None:
    Service(
        scope,
        "service",
        metadata=metadata(_NAME, _NAMESPACE),
        selector=deployment,
        ports=[ServicePort(name="http", port=_PORT, target_port=_PORT, protocol=Protocol.TCP)],
    )


def _add_http_route(scope: Construct) -> None:
    https_route(
        scope,
        "httproute",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="agentplane-dex-testing.allegedly.works",
        backend=_NAME,
        port=_PORT,
    )


def _add_network_policy(scope: Construct) -> None:
    cilium_helpers.network_policy(
        scope,
        "networkpolicy",
        metadata=metadata(_NAME, _NAMESPACE),
        selector=_LABELS,
        ingress=[
            cilium_helpers.ingress_from_gateway(_PORT),
            cilium_helpers.ingress_from(cilium_helpers.endpoint_labels(_NAMESPACE, "agentplane-app"), ports=[_PORT]),
            cilium_helpers.ingress_from(
                cilium_helpers.endpoint_labels(_NAMESPACE, "agentplane-oauth-fixture"), ports=[_PORT]
            ),
        ],
        egress=[cilium_helpers.dns_egress()],
        egress_deny=cilium_helpers.deny_all_egress(),
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
