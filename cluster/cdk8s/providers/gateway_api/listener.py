"""Ergonomic building blocks for Gateway API's `Gateway` listeners, following
cdk8s-plus's own construction pattern: named `@staticmethod` factories grouping a spec
fragment's real variant shapes under one type, each returning the generated struct. No
Gateway name/namespace, hostname, or certificate fact lives here -- those are this cluster's
own values, built in `cluster.cdk8s.gateway`.
"""

from __future__ import annotations

from gateway_api_gateway_crds.io.k8s.networking.gateway import (
    GatewaySpecListeners,
    GatewaySpecListenersAllowedRoutes,
    GatewaySpecListenersTls,
    GatewaySpecListenersTlsCertificateRefs,
    GatewaySpecListenersTlsMode,
)


class ListenerTls:
    """A listener's TLS termination. Gateway API discriminates two real modes
    (`Terminate`, `Passthrough`); only termination from a single certificate Secret is
    wrapped here -- add a `Passthrough` factory the day this cluster needs one.
    """

    @staticmethod
    def terminate(certificate_secret_name: str) -> GatewaySpecListenersTls:
        return GatewaySpecListenersTls(
            mode=GatewaySpecListenersTlsMode.TERMINATE,
            certificate_refs=[GatewaySpecListenersTlsCertificateRefs(name=certificate_secret_name)],
        )


class Listener:
    """One `Gateway` listener; `name` is its `spec.listeners[].name`, the section a route's
    `parentRefs[].sectionName` attaches to. Only the two protocol shapes this cluster builds
    today -- a TLS-terminating HTTPS listener and a plaintext HTTP listener -- are wrapped
    here; pass a full `GatewaySpecListeners` directly for any other combination.
    """

    @staticmethod
    def https(
        *,
        name: str,
        hostname: str,
        port: int,
        tls: GatewaySpecListenersTls,
        allowed_routes: GatewaySpecListenersAllowedRoutes | None = None,
    ) -> GatewaySpecListeners:
        return GatewaySpecListeners(
            name=name, hostname=hostname, port=port, protocol="HTTPS", tls=tls, allowed_routes=allowed_routes
        )

    @staticmethod
    def http(
        *, name: str, port: int, allowed_routes: GatewaySpecListenersAllowedRoutes | None = None
    ) -> GatewaySpecListeners:
        return GatewaySpecListeners(name=name, port=port, protocol="HTTP", allowed_routes=allowed_routes)
