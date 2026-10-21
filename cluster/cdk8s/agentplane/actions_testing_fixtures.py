"""Testing-only Action Service fixtures: the credentialless `mcp-everything` reference
server and the Dex-backed `oauth-fixture` MCP server, both acceptance-testing the MCP
linkage flow without touching any real credential. The k3s fixture runs the same workloads
without their Cilium policies.
"""

from __future__ import annotations

from pathlib import Path

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
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Probe,
    Protocol,
    Secret,
    Service,
    ServicePort,
)
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEndpointSelector,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressions,
    CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator,
)
from constructs import Construct

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.mcp_settings import McpOAuthServer
from cluster.cdk8s import cilium, pod_policy
from cluster.cdk8s.agentplane import actions, dex
from cluster.cdk8s.forgejo_registry import pull_credentials
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy, deny_all_egress

_NAMESPACE = "agentplane-testing"

_MCP_EVERYTHING_NAME = "mcp-everything"
# Keep in step with MODULE.bazel's `mcp_everything` pull, the build the k3s fixture runs.
_MCP_EVERYTHING_IMAGE = (
    "docker.io/tzolov/mcp-everything-server@sha256:96c4aa07420dd2a8dee0315763a8ea27de72fd054483c781894f6280cd3f56e7"
)
_MCP_EVERYTHING_PORT = 3001
_MCP_EVERYTHING_LABELS = {"app.kubernetes.io/name": _MCP_EVERYTHING_NAME}

OAUTH_FIXTURE_NAME = "oauth-fixture"
# The tag is always overridden by image-pins/kustomization.yaml; the k3s fixture imports its build under it.
_OAUTH_FIXTURE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture:unset"
OAUTH_FIXTURE_PORT = 8080
_OAUTH_FIXTURE_LABELS = {"app.kubernetes.io/name": OAUTH_FIXTURE_NAME}
_MCP_EVERYTHING_URL = f"http://{_MCP_EVERYTHING_NAME}.{_NAMESPACE}.svc.cluster.local:{_MCP_EVERYTHING_PORT}/mcp"
_OAUTH_FIXTURE_MCP_URL = f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}/mcp"
_CPU_LIMIT = Cpu.millis(250)
_MEMORY_LIMIT = Size.mebibytes(256)


def _resources(cpu_limit: Cpu | None, memory_limit: Size | None) -> ContainerResources:
    return ContainerResources(
        cpu=CpuResources(request=Cpu.millis(25), limit=cpu_limit),
        memory=MemoryResources(request=Size.mebibytes(128), limit=memory_limit),
        # EphemeralStorageResources only accepts whole gibibytes (cdk8s-plus
        # container.ts: `toGibibytes().toString() + 'Gi'`), and JsonPatch can't reach
        # the field: container resources are re-rendered from the construct's own
        # props after patches apply, so a patch into `containers/N/resources` is
        # silently discarded.
        ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
    )


def add_mcp_everything(
    scope: Construct,
    *,
    image: str = _MCP_EVERYTHING_IMAGE,
    image_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
    cpu_limit: Cpu | None = _CPU_LIMIT,
    memory_limit: Size | None = _MEMORY_LIMIT,
) -> None:
    """The `mcp-everything` Deployment and Service."""
    deployment = Deployment(
        scope,
        "mcp-everything-deployment",
        metadata=ApiObjectMetadata(name=_MCP_EVERYTHING_NAME, namespace=_NAMESPACE),
        pod_metadata=ApiObjectMetadata(labels=_MCP_EVERYTHING_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=image,
        image_pull_policy=image_pull_policy,
        command=["node", "dist/index.js", "streamableHttp"],
        ports=[ContainerPort(name="http", number=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=_MCP_EVERYTHING_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=_MCP_EVERYTHING_PORT, initial_delay_seconds=Duration.seconds(20), period_seconds=Duration.seconds(30)
        ),
        resources=_resources(cpu_limit, memory_limit),
        security_context=ContainerSecurityContextProps(
            read_only_root_filesystem=True, capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL])
        ),
    )
    pod_policy.harden(deployment)
    Service(
        scope,
        "mcp-everything-service",
        metadata=ApiObjectMetadata(name=_MCP_EVERYTHING_NAME, namespace=_NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=_MCP_EVERYTHING_PORT, target_port=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)
        ],
    )


def _add_mcp_everything_policies(scope: Construct) -> None:
    # Only the testing control-plane callers may reach this no-auth upstream reference
    # server. Runner isolation stays unchanged; there is no public route or fixture egress.

    NetworkPolicy(
        scope,
        "mcp-everything-networkpolicy",
        metadata=ApiObjectMetadata(name=_MCP_EVERYTHING_NAME.removeprefix("agentplane-"), namespace=_NAMESPACE),
        endpoint_selector=_MCP_EVERYTHING_LABELS,
        ingress=[
            IngressRule.from_endpoints(
                cilium.endpoint_labels(_NAMESPACE, "app"),
                cilium.endpoint_labels(_NAMESPACE, "actions"),
                ports=[_MCP_EVERYTHING_PORT],
            )
        ],
        egress_deny=deny_all_egress(),
    )
    # Add only this destination to the callers' existing egress fences.

    NetworkPolicy(
        scope,
        "mcp-everything-callers-networkpolicy",
        metadata=ApiObjectMetadata(
            name=f"{_MCP_EVERYTHING_NAME.removeprefix('agentplane-')}-callers", namespace=_NAMESPACE
        ),
        endpoint_selector=CiliumNetworkPolicySpecEndpointSelector(
            match_expressions=[
                CiliumNetworkPolicySpecEndpointSelectorMatchExpressions(
                    key="app.kubernetes.io/name",
                    operator=CiliumNetworkPolicySpecEndpointSelectorMatchExpressionsOperator.IN,
                    values=["app", "actions"],
                )
            ]
        ),
        egress=[
            EgressRule.to_endpoints(cilium.endpoint_labels(_NAMESPACE, _MCP_EVERYTHING_NAME), _MCP_EVERYTHING_PORT)
        ],
    )


def add_oauth_fixture(
    scope: Construct,
    *,
    authorization_server: str,
    jwks_uri: str,
    audience: str,
    registry_secret_name: str | None = pull_credentials.SECRET_NAME,
    image_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
    cpu_limit: Cpu | None = _CPU_LIMIT,
    memory_limit: Size | None = _MEMORY_LIMIT,
) -> None:
    """The `oauth-fixture` Deployment and Service: an MCP server accepting `authorization_server`'s
    JWTs for `audience`, verified with the keys at `jwks_uri`."""
    deployment = Deployment(
        scope,
        "oauth-fixture-deployment",
        metadata=ApiObjectMetadata(
            name=OAUTH_FIXTURE_NAME,
            namespace=_NAMESPACE,
            annotations={
                "description": "Dex-backed OAuth-protected MCP server for acceptance-testing MCP OAuth linkage; the fixture verifies Dex JWTs locally and has no credentials."
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_OAUTH_FIXTURE_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        docker_registry_auth=(
            Secret.from_secret_name(scope, "oauth-fixture-forgejo-images-creds-ref", registry_secret_name)
            if registry_secret_name is not None
            else None
        ),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=_OAUTH_FIXTURE_IMAGE,
        image_pull_policy=image_pull_policy,
        env_variables={
            # The MCP resource server remains cluster-internal.
            "OAUTH_FIXTURE_BASE_URL": EnvValue.from_value(
                f"http://{OAUTH_FIXTURE_NAME}.{_NAMESPACE}.svc.cluster.local:{OAUTH_FIXTURE_PORT}"
            ),
            "OAUTH_FIXTURE_AUTHORIZATION_SERVER": EnvValue.from_value(authorization_server),
            "OAUTH_FIXTURE_JWKS_URI": EnvValue.from_value(jwks_uri),
            "OAUTH_FIXTURE_AUDIENCE": EnvValue.from_value(audience),
        },
        ports=[ContainerPort(name="http", number=OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=OAUTH_FIXTURE_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=OAUTH_FIXTURE_PORT, initial_delay_seconds=Duration.seconds(15), period_seconds=Duration.seconds(30)
        ),
        resources=_resources(cpu_limit, memory_limit),
        # No readOnlyRootFilesystem: the aspect_rules_py launcher materialises its venv
        # at startup inside the image's own runfiles directory, so the root filesystem
        # must stay writable (see cluster/cdk8s/ssh_mcp/backend.py for the same
        # constraint).
        security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
    )
    pod_policy.harden(deployment)
    Service(
        scope,
        "oauth-fixture-service",
        metadata=ApiObjectMetadata(name=OAUTH_FIXTURE_NAME, namespace=_NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=OAUTH_FIXTURE_PORT, target_port=OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)
        ],
    )


def _add_oauth_fixture_policy(scope: Construct) -> None:
    # Cluster-internal only, no public route. The Action Service reaches it for
    # protected-resource discovery and tool calls. The fixture fetches Dex's signing
    # keys through the internal Service; its public Dex issuer is metadata only.

    NetworkPolicy(
        scope,
        "oauth-fixture-networkpolicy",
        metadata=ApiObjectMetadata(name=OAUTH_FIXTURE_NAME.removeprefix("agentplane-"), namespace=_NAMESPACE),
        endpoint_selector=_OAUTH_FIXTURE_LABELS,
        ingress=[IngressRule.from_endpoints(cilium.endpoint_labels(_NAMESPACE, "actions"), ports=[OAUTH_FIXTURE_PORT])],
        egress=[cilium.dns_egress(resolves=["*"]), dex.service().egress()],
        egress_deny=deny_all_egress(),
    )


def everything_group() -> ActionGroup:
    """An Action group calling `mcp-everything`."""
    return ActionGroup(
        title="Upstream Everything",
        description="Credentialless MCP reference server for testing acceptance.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="Community-built Everything image; no user account, workload token, or mounted credentials.",
            config={"transport": "streamable-http", "url": _MCP_EVERYTHING_URL, "auth": "none"},
        ),
    )


def oauth_fixture_server(server_id: str, *, client_id: str, redirect_uri: str) -> McpOAuthServer:
    """`oauth-fixture` as the MCP server `server_id`, linked through the Dex client `client_id`, whose
    secret the Action Service mounts from `actions.mcp_oauth_secret`."""
    return McpOAuthServer(
        server_id=server_id,
        server_url=_OAUTH_FIXTURE_MCP_URL,
        client_id=client_id,
        client_secret_file=Path(actions.MCP_OAUTH_DIR, "client-secret"),
        redirect_uri=redirect_uri,
        scopes=["openid"],
    )


def oauth_fixture_group(server_id: str) -> ActionGroup:
    """An Action group calling `oauth-fixture` with the operator's linkage to the MCP server `server_id`."""
    return ActionGroup(
        title="OAuth Example",
        description="Dex-backed OAuth-linked MCP fixture for testing acceptance of the linkage flow.",
        executor=McpExecutorBinding(
            kind="mcp",
            description="MCP tool protected by Dex-issued JWTs; no real credentials.",
            config={
                "transport": "streamable-http",
                "url": _OAUTH_FIXTURE_MCP_URL,
                "server_id": server_id,
                "auth": "oauth",
            },
        ),
    )


def add_testing_fixtures(scope: Construct) -> None:
    add_mcp_everything(scope)
    _add_mcp_everything_policies(scope)
    add_oauth_fixture(
        scope,
        # Dex's public issuer is advertised to clients, while the fixture fetches signing keys
        # over the internal Dex Service.
        authorization_server="https://agentplane-dex-testing.allegedly.works/dex",
        jwks_uri=f"http://{dex.service().fqdn}:{dex.service().port.number}/dex/keys",
        audience="agentplane-testing-mcp",
    )
    _add_oauth_fixture_policy(scope)
