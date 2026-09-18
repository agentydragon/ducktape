"""Testing-only Action Service fixtures: the credentialless `mcp-everything` reference
server and the Dex-backed `oauth-fixture` MCP server, both acceptance-testing the MCP
linkage flow without touching any real credential.
"""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, Duration, JsonPatch, Size
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
    EphemeralStorageResources,
    MemoryResources,
    PodSecurityContextProps,
    Probe,
    Protocol,
    Secret,
    Service,
    ServicePort,
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
    CiliumNetworkPolicySpecEgressToPortsRules,
    CiliumNetworkPolicySpecEgressToPortsRulesDns,
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
    CiliumNetworkPolicySpecIngress,
    CiliumNetworkPolicySpecIngressFromEndpoints,
    CiliumNetworkPolicySpecIngressToPorts,
    CiliumNetworkPolicySpecIngressToPortsPorts,
    CiliumNetworkPolicySpecIngressToPortsPortsProtocol,
)
from constructs import Construct

from cluster.cdk8s.metadata import metadata

_NAMESPACE = "agentplane-testing"

_MCP_EVERYTHING_NAME = "agentplane-mcp-everything"
_MCP_EVERYTHING_IMAGE = (
    "docker.io/tzolov/mcp-everything-server@sha256:96c4aa07420dd2a8dee0315763a8ea27de72fd054483c781894f6280cd3f56e7"
)
_MCP_EVERYTHING_PORT = 3001
_MCP_EVERYTHING_LABELS = {"app.kubernetes.io/name": _MCP_EVERYTHING_NAME}

_OAUTH_FIXTURE_NAME = "agentplane-oauth-fixture"
_OAUTH_FIXTURE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture"
_OAUTH_FIXTURE_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_OAUTH_FIXTURE_PORT = 8080
_OAUTH_FIXTURE_LABELS = {"app.kubernetes.io/name": _OAUTH_FIXTURE_NAME}


def _add_mcp_everything(scope: Construct) -> None:
    deployment = Deployment(
        scope,
        "mcp-everything-deployment",
        metadata=metadata(_MCP_EVERYTHING_NAME, _NAMESPACE),
        pod_metadata=ApiObjectMetadata(labels=_MCP_EVERYTHING_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=_MCP_EVERYTHING_IMAGE,
        command=["node", "dist/index.js", "streamableHttp"],
        ports=[ContainerPort(name="http", number=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=_MCP_EVERYTHING_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=_MCP_EVERYTHING_PORT, initial_delay_seconds=Duration.seconds(20), period_seconds=Duration.seconds(30)
        ),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(250)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            # EphemeralStorageResources only accepts whole gibibytes (cdk8s-plus
            # container.ts: `toGibibytes().toString() + 'Gi'`) -- the original
            # hand-written 16Mi/128Mi rounds up to the smallest expressible value.
            # JsonPatch can't reach this field either: container resources are
            # re-rendered from the construct's own props after patches apply, so a
            # patch into `containers/N/resources` is silently discarded.
            ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
        ),
        security_context=ContainerSecurityContextProps(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
        ),
    )
    # cdk8s_plus_34's PodSecurityContextProps has no seccompProfile builder -- patch the
    # pod-level field directly (same escape hatch used elsewhere for this exact gap; see
    # llm_ingress_constructs.py).
    ApiObject.of(deployment).add_json_patch(
        JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", {"type": "RuntimeDefault"})
    )
    Service(
        scope,
        "mcp-everything-service",
        metadata=metadata(_MCP_EVERYTHING_NAME, _NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=_MCP_EVERYTHING_PORT, target_port=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)
        ],
    )
    # Only the testing control-plane callers may reach this no-auth upstream reference
    # server. Runner isolation stays unchanged; there is no public route or fixture egress.
    CiliumNetworkPolicy(
        scope,
        "mcp-everything-networkpolicy",
        metadata=metadata(_MCP_EVERYTHING_NAME, _NAMESPACE),
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_MCP_EVERYTHING_LABELS),
            ingress=[
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=[
                        CiliumNetworkPolicySpecIngressFromEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-app",
                            }
                        ),
                        CiliumNetworkPolicySpecIngressFromEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-actions",
                            }
                        ),
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecIngressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecIngressToPortsPorts(
                                    port=str(_MCP_EVERYTHING_PORT),
                                    protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                )
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
    # Add only this destination to the callers' existing egress fences.
    CiliumNetworkPolicy(
        scope,
        "mcp-everything-callers-networkpolicy",
        metadata=metadata(f"{_MCP_EVERYTHING_NAME}-callers", _NAMESPACE),
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(
                match_expressions=[
                    CiliumNetworkPolicySpecEndpointSelectorMatchExpressions(
                        key="app.kubernetes.io/name",
                        operator=CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                        values=["agentplane-app", "agentplane-actions"],
                    )
                ]
            ),
            egress=[
                CiliumNetworkPolicySpecEgress(
                    to_endpoints=[
                        CiliumNetworkPolicySpecEgressToEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": _MCP_EVERYTHING_NAME,
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecEgressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecEgressToPortsPorts(
                                    port=str(_MCP_EVERYTHING_PORT),
                                    protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP,
                                )
                            ]
                        )
                    ],
                )
            ],
        ),
    )


def _add_oauth_fixture(scope: Construct) -> None:
    deployment = Deployment(
        scope,
        "oauth-fixture-deployment",
        metadata=metadata(
            _OAUTH_FIXTURE_NAME,
            _NAMESPACE,
            annotations={
                "description": "Dex-backed OAuth-protected MCP server for acceptance-testing MCP OAuth linkage; the fixture verifies Dex JWTs locally and has no credentials."
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_OAUTH_FIXTURE_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        docker_registry_auth=Secret.from_secret_name(
            scope, "oauth-fixture-forgejo-images-creds-ref", "forgejo-images-creds"
        ),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=f"{_OAUTH_FIXTURE_IMAGE}:{_OAUTH_FIXTURE_PLACEHOLDER_TAG}",
        env_variables={
            # The MCP resource server remains cluster-internal. Dex's public issuer is
            # advertised to clients, while the fixture fetches signing keys over the
            # internal Dex Service.
            "OAUTH_FIXTURE_BASE_URL": EnvValue.from_value(
                f"http://{_OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{_OAUTH_FIXTURE_PORT}"
            ),
            "OAUTH_FIXTURE_AUTHORIZATION_SERVER": EnvValue.from_value(
                "https://agentplane-dex-testing.allegedly.works/dex"
            ),
            "OAUTH_FIXTURE_JWKS_URI": EnvValue.from_value(
                f"http://agentplane-testing-dex.{_NAMESPACE}.svc.cluster.local:5556/dex/keys"
            ),
            "OAUTH_FIXTURE_AUDIENCE": EnvValue.from_value("agentplane-testing-mcp"),
        },
        ports=[ContainerPort(name="http", number=_OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=_OAUTH_FIXTURE_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=_OAUTH_FIXTURE_PORT, initial_delay_seconds=Duration.seconds(15), period_seconds=Duration.seconds(30)
        ),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(250)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            # See _add_mcp_everything's matching comment: EphemeralStorageResources
            # can't express the original 16Mi/128Mi, so this rounds up to 1Gi.
            ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
        ),
        # No readOnlyRootFilesystem: the aspect_rules_py launcher materialises its venv
        # at startup inside the image's own runfiles directory, so the root filesystem
        # must stay writable (see cluster/k8s/ssh-mcp/deployment.yaml for the same
        # constraint).
        security_context=ContainerSecurityContextProps(
            allow_privilege_escalation=False,
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
            read_only_root_filesystem=False,
        ),
    )
    # See _add_mcp_everything's matching comment: seccompProfile has no
    # PodSecurityContextProps builder.
    ApiObject.of(deployment).add_json_patch(
        JsonPatch.add("/spec/template/spec/securityContext/seccompProfile", {"type": "RuntimeDefault"})
    )
    Service(
        scope,
        "oauth-fixture-service",
        metadata=metadata(_OAUTH_FIXTURE_NAME, _NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=_OAUTH_FIXTURE_PORT, target_port=_OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)
        ],
    )
    # Cluster-internal only, no public route. The Action Service reaches it for
    # protected-resource discovery and tool calls. The fixture fetches Dex's signing
    # keys through the internal Service; its public Dex issuer is metadata only.
    CiliumNetworkPolicy(
        scope,
        "oauth-fixture-networkpolicy",
        metadata=metadata(_OAUTH_FIXTURE_NAME, _NAMESPACE),
        spec=CiliumNetworkPolicySpec(
            endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(match_labels=_OAUTH_FIXTURE_LABELS),
            ingress=[
                CiliumNetworkPolicySpecIngress(
                    from_endpoints=[
                        CiliumNetworkPolicySpecIngressFromEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-actions",
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecIngressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecIngressToPortsPorts(
                                    port=str(_OAUTH_FIXTURE_PORT),
                                    protocol=CiliumNetworkPolicySpecIngressToPortsPortsProtocol.TCP,
                                )
                            ]
                        )
                    ],
                )
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
                            ],
                            rules=CiliumNetworkPolicySpecEgressToPortsRules(
                                dns=[CiliumNetworkPolicySpecEgressToPortsRulesDns(match_pattern="*")]
                            ),
                        )
                    ],
                ),
                CiliumNetworkPolicySpecEgress(
                    to_endpoints=[
                        CiliumNetworkPolicySpecEgressToEndpoints(
                            match_labels={
                                "k8s:io.kubernetes.pod.namespace": _NAMESPACE,
                                "app.kubernetes.io/name": "agentplane-testing-dex",
                            }
                        )
                    ],
                    to_ports=[
                        CiliumNetworkPolicySpecEgressToPorts(
                            ports=[
                                CiliumNetworkPolicySpecEgressToPortsPorts(
                                    port="5556", protocol=CiliumNetworkPolicySpecEgressToPortsPortsProtocol.TCP
                                )
                            ]
                        )
                    ],
                ),
            ],
            egress_deny=[
                CiliumNetworkPolicySpecEgressDeny(to_entities=[CiliumNetworkPolicySpecEgressDenyToEntities.ALL])
            ],
        ),
    )


def add_testing_fixtures(scope: Construct) -> None:
    _add_mcp_everything(scope)
    _add_oauth_fixture(scope)
