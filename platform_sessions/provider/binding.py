"""Assemble one remotely owned integration without replacing gateway authorization."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import grpc
from pydantic import SecretStr

from platform_sessions.provider.control_client import ProviderControlClient, dial_provider_control
from platform_sessions.provider.management_adapter import RemoteIntegration
from platform_sessions.proxy import HeaderPolicy, ProxyInstance
from platform_sessions.registry import IntegrationInstance
from platform_sessions.remote_transport import RemoteSessionTransport


@dataclass(frozen=True)
class RemoteProviderSpec:
    """Fixed provider endpoints and projected gateway-to-provider credentials."""

    config: IntegrationInstance
    provider_origin: str
    control_target: str
    control_token_file: Path = field(repr=False)
    control_timeout_s: float
    owner_token: SecretStr = field(repr=False)
    route_token: SecretStr = field(repr=False)
    headers: HeaderPolicy = field(default_factory=HeaderPolicy)
    tls_context: ssl.SSLContext | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RemoteProviderBinding:
    """Objects to pass to create_app and install_proxy using the gateway's auth."""

    integration: RemoteIntegration
    proxy_instance: ProxyInstance


def bind_remote_provider(
    spec: RemoteProviderSpec, *, control_channel: grpc.aio.Channel, http_transport=None
) -> RemoteProviderBinding:
    """Bind to a caller-owned channel; the caller must disable gRPC retries."""
    if not spec.config.enabled:
        raise ValueError("Cannot bind a disabled integration")
    control = ProviderControlClient(
        control_channel,
        provider_id=spec.config.type,
        instance_id=spec.config.id,
        token_file=spec.control_token_file,
        timeout_s=spec.control_timeout_s,
    )
    transport = RemoteSessionTransport(
        instance_id=spec.config.id,
        upstream_base_url=spec.config.upstream_base_url,
        provider_base_url=spec.provider_origin,
        owner_token=spec.owner_token,
        route_token=spec.route_token,
        tls_context=spec.tls_context,
        transport=http_transport,
    )
    return RemoteProviderBinding(
        integration=RemoteIntegration(spec.config, control),
        proxy_instance=ProxyInstance(spec.config, transport, spec.headers),
    )


@asynccontextmanager
async def open_remote_provider(
    spec: RemoteProviderSpec,
    *,
    tls_credentials: grpc.ChannelCredentials | None = None,
    control_options: Sequence[tuple[str, int | str]] = (),
    control_channel: grpc.aio.Channel | None = None,
    http_transport=None,
) -> AsyncIterator[RemoteProviderBinding]:
    """Use a retry-disabled TLS channel, or borrow an injected caller-owned channel.

    Channels constructed here close on exit. Injected channels stay owned by the
    caller, which must also disable gRPC retries and close them separately.
    """
    owned = control_channel is None
    channel = control_channel or dial_provider_control(
        spec.control_target, credentials=tls_credentials, options=control_options
    )
    try:
        yield bind_remote_provider(spec, control_channel=channel, http_transport=http_transport)
    finally:
        if owned:
            await channel.close()
