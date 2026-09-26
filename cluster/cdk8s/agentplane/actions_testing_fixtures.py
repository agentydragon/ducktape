"""Testing-only Action Service fixtures: the credentialless `mcp-everything` reference
server and the Dex-backed `oauth-fixture` MCP server, both acceptance-testing the MCP
linkage flow without touching any real credential.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
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
    Service,
    ServicePort,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import container_security
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy, deny_all_egress

_NAMESPACE = "agentplane-testing"

MCP_EVERYTHING_NAME = "agentplane-mcp-everything"
_MCP_EVERYTHING_IMAGE = (
    "docker.io/tzolov/mcp-everything-server@sha256:96c4aa07420dd2a8dee0315763a8ea27de72fd054483c781894f6280cd3f56e7"
)
MCP_EVERYTHING_PORT = 3001
_MCP_EVERYTHING_LABELS = {"app.kubernetes.io/name": MCP_EVERYTHING_NAME}

OAUTH_FIXTURE_NAME = "agentplane-oauth-fixture"
_OAUTH_FIXTURE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture"
_OAUTH_FIXTURE_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
OAUTH_FIXTURE_PORT = 8080
_OAUTH_FIXTURE_LABELS = {"app.kubernetes.io/name": OAUTH_FIXTURE_NAME}


def _add_mcp_everything(scope: Construct) -> None:
    deployment = Deployment(
        scope,
        "mcp-everything-deployment",
        metadata=metadata(MCP_EVERYTHING_NAME, _NAMESPACE),
        pod_metadata=ApiObjectMetadata(labels=_MCP_EVERYTHING_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=_MCP_EVERYTHING_IMAGE,
        command=["node", "dist/index.js", "streamableHttp"],
        ports=[ContainerPort(name="http", number=MCP_EVERYTHING_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=MCP_EVERYTHING_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=MCP_EVERYTHING_PORT, initial_delay_seconds=Duration.seconds(20), period_seconds=Duration.seconds(30)
        ),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(250)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            # EphemeralStorageResources only accepts whole gibibytes (cdk8s-plus
            # container.ts: `toGibibytes().toString() + 'Gi'`), and JsonPatch can't reach
            # the field: container resources are re-rendered from the construct's own
            # props after patches apply, so a patch into `containers/N/resources` is
            # silently discarded.
            ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
        ),
        security_context=ContainerSecurityContextProps(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL]),
        ),
    )
    apply_pod_spec_patches(deployment)
    Service(
        scope,
        "mcp-everything-service",
        metadata=metadata(MCP_EVERYTHING_NAME, _NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=MCP_EVERYTHING_PORT, target_port=MCP_EVERYTHING_PORT, protocol=Protocol.TCP)
        ],
    )
    # Only the testing control-plane callers may reach this no-auth upstream reference
    # server. Runner isolation stays unchanged; there is no public route or fixture egress.

    NetworkPolicy(
        scope,
        "mcp-everything-networkpolicy",
        metadata=metadata(MCP_EVERYTHING_NAME, _NAMESPACE),
        selector=_MCP_EVERYTHING_LABELS,
        ingress=[
            IngressRule.from_endpoints(
                cilium.endpoint_labels(_NAMESPACE, "agentplane-app"),
                cilium.endpoint_labels(_NAMESPACE, "agentplane-actions"),
                ports=[MCP_EVERYTHING_PORT],
            )
        ],
        egress_deny=deny_all_egress(),
    )
    # Add only this destination to the callers' existing egress fences.

    NetworkPolicy(
        scope,
        "mcp-everything-callers-networkpolicy",
        metadata=metadata(f"{MCP_EVERYTHING_NAME}-callers", _NAMESPACE),
        selector=CiliumNetworkPolicySpecEndpointSelector(
            match_expressions=[
                CiliumNetworkPolicySpecEndpointSelectorMatchExpressions(
                    key="app.kubernetes.io/name",
                    operator=CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                    values=["agentplane-app", "agentplane-actions"],
                )
            ]
        ),
        egress=[EgressRule.to_endpoints(cilium.endpoint_labels(_NAMESPACE, MCP_EVERYTHING_NAME), MCP_EVERYTHING_PORT)],
    )


def _add_oauth_fixture(scope: Construct) -> None:
    deployment = Deployment(
        scope,
        "oauth-fixture-deployment",
        metadata=metadata(
            OAUTH_FIXTURE_NAME,
            _NAMESPACE,
            annotations={
                "description": "Dex-backed OAuth-protected MCP server for acceptance-testing MCP OAuth linkage; the fixture verifies Dex JWTs locally and has no credentials."
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_OAUTH_FIXTURE_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        docker_registry_auth=forgejo_images_creds_secret_ref(scope, "oauth-fixture-forgejo-images-creds-ref"),
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
                f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}"
            ),
            "OAUTH_FIXTURE_AUTHORIZATION_SERVER": EnvValue.from_value(
                "https://agentplane-dex-testing.allegedly.works/dex"
            ),
            "OAUTH_FIXTURE_JWKS_URI": EnvValue.from_value(
                f"http://agentplane-testing-dex.{_NAMESPACE}.svc.cluster.local:5556/dex/keys"
            ),
            "OAUTH_FIXTURE_AUDIENCE": EnvValue.from_value("agentplane-testing-mcp"),
        },
        ports=[ContainerPort(name="http", number=OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=OAUTH_FIXTURE_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=OAUTH_FIXTURE_PORT, initial_delay_seconds=Duration.seconds(15), period_seconds=Duration.seconds(30)
        ),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(25), limit=Cpu.millis(250)),
            memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(256)),
            # Whole gibibytes only, as in _add_mcp_everything.
            ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
        ),
        # No readOnlyRootFilesystem: the aspect_rules_py launcher materialises its venv
        # at startup inside the image's own runfiles directory, so the root filesystem
        # must stay writable (see cluster/cdk8s/ssh_mcp/backend.py for the same
        # constraint).
        security_context=container_security.WRITABLE_ROOT,
    )
    apply_pod_spec_patches(deployment)
    Service(
        scope,
        "oauth-fixture-service",
        metadata=metadata(OAUTH_FIXTURE_NAME, _NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=OAUTH_FIXTURE_PORT, target_port=OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)
        ],
    )
    # Cluster-internal only, no public route. The Action Service reaches it for
    # protected-resource discovery and tool calls. The fixture fetches Dex's signing
    # keys through the internal Service; its public Dex issuer is metadata only.

    NetworkPolicy(
        scope,
        "oauth-fixture-networkpolicy",
        metadata=metadata(OAUTH_FIXTURE_NAME, _NAMESPACE),
        selector=_OAUTH_FIXTURE_LABELS,
        ingress=[
            IngressRule.from_endpoints(
                cilium.endpoint_labels(_NAMESPACE, "agentplane-actions"), ports=[OAUTH_FIXTURE_PORT]
            )
        ],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            EgressRule.to_endpoints(cilium.endpoint_labels(_NAMESPACE, "agentplane-testing-dex"), 5556),
        ],
        egress_deny=deny_all_egress(),
    )


def add_testing_fixtures(scope: Construct) -> None:
    _add_mcp_everything(scope)
    _add_oauth_fixture(scope)
