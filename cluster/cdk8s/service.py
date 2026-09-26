"""Builds the single-port `Service` most workloads need."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import Deployment, Protocol, Service, ServicePort
from constructs import Construct


def simple_http_service(
    scope: Construct, id: str, *, metadata: ApiObjectMetadata, deployment: Deployment, port: int, name: str = "http"
) -> Service:
    return Service(
        scope,
        id,
        metadata=metadata,
        selector=deployment,
        ports=[ServicePort(name=name, port=port, target_port=port, protocol=Protocol.TCP)],
    )
