"""The MCP servers the k3s stack's Action groups call: the credentialless `mcp-everything` reference
server and the Dex-backed `oauth-fixture` MCP server, which test the MCP linkage flow without touching
any real credential.
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
    Service,
    ServicePort,
)
from constructs import Construct

from agentplane.action_service.catalog import ActionGroup, McpExecutorBinding
from agentplane.action_service.mcp_settings import McpOAuthServer
from agentplane.system_tests.k3s.namespaces import NAMESPACE
from cluster.cdk8s import pod_policy
from cluster.cdk8s.agentplane import actions

_MCP_EVERYTHING_NAME = "mcp-everything"
_MCP_EVERYTHING_PORT = 3001
_MCP_EVERYTHING_LABELS = {"app.kubernetes.io/name": _MCP_EVERYTHING_NAME}

_OAUTH_FIXTURE_NAME = "oauth-fixture"
# The k3s fixture imports its build under this name.
_OAUTH_FIXTURE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-oauth-fixture:unset"
_OAUTH_FIXTURE_PORT = 8080
_OAUTH_FIXTURE_LABELS = {"app.kubernetes.io/name": _OAUTH_FIXTURE_NAME}
_MCP_EVERYTHING_URL = f"http://{_MCP_EVERYTHING_NAME}.{NAMESPACE}.svc.cluster.local:{_MCP_EVERYTHING_PORT}/mcp"
_OAUTH_FIXTURE_MCP_URL = f"http://{_OAUTH_FIXTURE_NAME}.{NAMESPACE}.svc.cluster.local:{_OAUTH_FIXTURE_PORT}/mcp"


def _resources() -> ContainerResources:
    return ContainerResources(
        cpu=CpuResources(request=Cpu.millis(25)),
        memory=MemoryResources(request=Size.mebibytes(128)),
        # EphemeralStorageResources only accepts whole gibibytes (cdk8s-plus
        # container.ts: `toGibibytes().toString() + 'Gi'`), and JsonPatch can't reach
        # the field: container resources are re-rendered from the construct's own
        # props after patches apply, so a patch into `containers/N/resources` is
        # silently discarded.
        ephemeral_storage=EphemeralStorageResources(request=Size.gibibytes(1), limit=Size.gibibytes(1)),
    )


def add_mcp_everything(scope: Construct, *, image: str) -> None:
    """The `mcp-everything` Deployment and Service, running `image`."""
    deployment = Deployment(
        scope,
        "mcp-everything-deployment",
        metadata=ApiObjectMetadata(name=_MCP_EVERYTHING_NAME, namespace=NAMESPACE),
        pod_metadata=ApiObjectMetadata(labels=_MCP_EVERYTHING_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=image,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        command=["node", "dist/index.js", "streamableHttp"],
        ports=[ContainerPort(name="http", number=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=_MCP_EVERYTHING_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=_MCP_EVERYTHING_PORT, initial_delay_seconds=Duration.seconds(20), period_seconds=Duration.seconds(30)
        ),
        resources=_resources(),
        security_context=ContainerSecurityContextProps(
            read_only_root_filesystem=True, capabilities=ContainerSecutiryContextCapabilities(drop=[Capability.ALL])
        ),
    )
    pod_policy.harden(deployment)
    Service(
        scope,
        "mcp-everything-service",
        metadata=ApiObjectMetadata(name=_MCP_EVERYTHING_NAME, namespace=NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=_MCP_EVERYTHING_PORT, target_port=_MCP_EVERYTHING_PORT, protocol=Protocol.TCP)
        ],
    )


def add_oauth_fixture(scope: Construct, *, authorization_server: str, jwks_uri: str, audience: str) -> None:
    """The `oauth-fixture` Deployment and Service: an MCP server accepting `authorization_server`'s
    JWTs for `audience`, verified with the keys at `jwks_uri`."""
    deployment = Deployment(
        scope,
        "oauth-fixture-deployment",
        metadata=ApiObjectMetadata(
            name=_OAUTH_FIXTURE_NAME,
            namespace=NAMESPACE,
            annotations={
                "description": "Dex-backed OAuth-protected MCP server for acceptance-testing MCP OAuth linkage; the fixture verifies Dex JWTs locally and has no credentials."
            },
        ),
        pod_metadata=ApiObjectMetadata(labels=_OAUTH_FIXTURE_LABELS),
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000),
    )
    deployment.add_container(
        name="fixture",
        image=_OAUTH_FIXTURE_IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        env_variables={
            # The MCP resource server remains cluster-internal.
            "OAUTH_FIXTURE_BASE_URL": EnvValue.from_value(
                f"http://{_OAUTH_FIXTURE_NAME}.{NAMESPACE}.svc.cluster.local:{_OAUTH_FIXTURE_PORT}"
            ),
            "OAUTH_FIXTURE_AUTHORIZATION_SERVER": EnvValue.from_value(authorization_server),
            "OAUTH_FIXTURE_JWKS_URI": EnvValue.from_value(jwks_uri),
            "OAUTH_FIXTURE_AUDIENCE": EnvValue.from_value(audience),
        },
        ports=[ContainerPort(name="http", number=_OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)],
        readiness=Probe.from_tcp_socket(port=_OAUTH_FIXTURE_PORT, period_seconds=Duration.seconds(5)),
        liveness=Probe.from_tcp_socket(
            port=_OAUTH_FIXTURE_PORT, initial_delay_seconds=Duration.seconds(15), period_seconds=Duration.seconds(30)
        ),
        resources=_resources(),
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
        metadata=ApiObjectMetadata(name=_OAUTH_FIXTURE_NAME, namespace=NAMESPACE),
        selector=deployment,
        ports=[
            ServicePort(name="http", port=_OAUTH_FIXTURE_PORT, target_port=_OAUTH_FIXTURE_PORT, protocol=Protocol.TCP)
        ],
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
