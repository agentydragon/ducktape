"""Reusable cdk8s constructs for agentplane-testing's dex/ directory: the Dex
Deployment, Service, HTTPRoute, NetworkPolicy, and the ESO-generated credentials
(4 Password generators + 3 ExternalSecrets, one of which carries the inline Dex
config.yaml -- built the same way litellm_config.py embeds a YAML config string
inside a ConfigMap's data, not a new mechanism).

Testing-only: staging has no Dex, it federates directly to the shared Authentik.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, JsonPatch, Size
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
from cilium_crds.io.cilium import (
    CiliumNetworkPolicy,
    CiliumNetworkPolicySpec,
    CiliumNetworkPolicySpecEgress,
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEgressDenyToEntities,
    CiliumNetworkPolicySpecEgressToEndpoints,
    CiliumNetworkPolicySpecEgressToPorts,
    CiliumNetworkPolicySpecEgressToPortsPorts,
    CiliumNetworkPolicySpecEgressToPortsPortsProtocol,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressFromEntities,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
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
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
)

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.probes import http_probe

_NAMESPACE = "agentplane-testing"
_NAME = "agentplane-testing-dex"
_IMAGE = "ghcr.io/dexidp/dex:v2.45.1"
_PORT = 5556
_LABELS = {"app.kubernetes.io/name": _NAME}
_ISSUER = "https://agentplane-dex-testing.allegedly.works/dex"


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
                    "email": "test-user@agentplane-testing.invalid",
                    "hash": '{{ regexReplaceAll "^test-user:" (htpasswd "test-user" .password "bcrypt") "" }}',
                    "username": "test-user",
                    "preferredUsername": "test-user",
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
                        "login": "test-user@agentplane-testing.invalid",
                        "username": "test-user",
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
        args=["dex", "serve", "/etc/dex/config.yaml"],
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
    deployment.containers[0].mount("/etc/dex", config_volume, read_only=True)
    deployment.containers[0].mount("/tmp", tmp_volume)

    # cdk8s_plus_34's PodSecurityContextProps has no seccompProfile builder -- patch the
    # pod-level field directly (same escape hatch used elsewhere for this exact gap; see
    # agentplane_llm_ingress_constructs.py).
    ApiObject.of(deployment).add_json_patch(
        JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", {"type": "RuntimeDefault"})
    )
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
    HttpRoute(
        scope,
        "httproute",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[
                HttpRouteSpecParentRefs(
                    name="cluster-gateway", namespace="gateway-system", section_name="https-wildcard"
                )
            ],
            hostnames=["agentplane-dex-testing.allegedly.works"],
            rules=[
                HttpRouteSpecRules(
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=_NAME, port=_PORT)],
                    filters=[
                        HttpRouteSpecRulesFilters(
                            type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                            response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                                set=[
                                    HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                                        name="Strict-Transport-Security", value="max-age=31536000"
                                    )
                                ]
                            ),
                        )
                    ],
                )
            ],
        ),
    )


def _add_network_policy(scope: Construct) -> None:
    CiliumNetworkPolicy(
        scope,
        "networkpolicy",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_LABELS),
            ingress=[
                CiliumNetworkPolicySpecIngress(
                    from_entities=[CiliumNetworkPolicySpecIngressFromEntities.INGRESS],
                    to_ports=[
                        CiliumNetworkPolicySpecIngressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecIngressToPortsPorts(
                                    port=str(_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                                )
                            ]
                        )
                    ],
                ),
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=[
                        CiliumNetworkPolicySpecIngressFromEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-app",
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecIngressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecIngressToPortsPorts(
                                    port=str(_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
                                )
                            ]
                        )
                    ],
                ),
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=[
                        CiliumNetworkPolicySpecIngressFromEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-oauth-fixture",
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecIngressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecIngressToPortsPorts(
                                    port=str(_PORT), protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP
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
                            match_labels={"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}
                        )
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
                )
            ],
            egress_deny=[
                CiliumNetworkPolicySpecEgressDeny(to_entities=[CiliumNetworkPolicySpecEgressDenyToEntities.ALL])
            ],
        ),
    )


class AgentplaneDex(Construct):
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
