"""The external MCP/OAuth paths admitted through the public Gateway."""

from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.providers.gateway_api.http_route import HttpRoute
from cluster.cdk8s.service_ref import ServiceRef

PUBLIC_PATHS = (
    "/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp",
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    "/auth/callback",
)


def add_public_route(scope: Construct, *, endpoint: ServiceRef, hostname: str) -> HttpRoute:
    return https_route(
        scope,
        "public-mcp-route",
        metadata=ApiObjectMetadata(name=f"{endpoint.name}-mcp", namespace=endpoint.pods.namespace),
        hostnames=[hostname],
        backend=endpoint,
        paths=PUBLIC_PATHS,
        timeout="3600s",
    )
