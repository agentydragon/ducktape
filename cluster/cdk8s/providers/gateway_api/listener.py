"""Ergonomic building blocks for Gateway API's `Gateway` listeners, following
cdk8s-plus's own construction pattern: named `@classmethod` factories grouping a spec
fragment's real variant shapes under one type. No Gateway name/namespace, hostname, or
certificate fact lives here -- those are this cluster's own values, built in
`cluster.cdk8s.gateway`.
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

    def __init__(self, spec: GatewaySpecListenersTls) -> None:
        self._spec = spec

    def to_spec(self) -> GatewaySpecListenersTls:
        return self._spec

    @classmethod
    def terminate(cls, certificate_secret_name: str) -> ListenerTls:
        return cls(
            GatewaySpecListenersTls(
                mode=GatewaySpecListenersTlsMode.TERMINATE,
                certificate_refs=[GatewaySpecListenersTlsCertificateRefs(name=certificate_secret_name)],
            )
        )


class Listener:
    """One `Gateway` listener. Only the two protocol shapes this cluster builds today
    -- a TLS-terminating HTTPS listener and a plaintext HTTP listener -- are wrapped
    here; pass a full `GatewaySpecListeners` directly for any other combination.
    """

    def __init__(self, spec: GatewaySpecListeners) -> None:
        self._spec = spec

    def to_spec(self) -> GatewaySpecListeners:
        return self._spec

    @classmethod
    def https(
        cls,
        *,
        name: str,
        hostname: str,
        port: int,
        tls: ListenerTls,
        allowed_routes: GatewaySpecListenersAllowedRoutes | None = None,
    ) -> Listener:
        return cls(
            GatewaySpecListeners(
                name=name,
                hostname=hostname,
                port=port,
                protocol="HTTPS",
                tls=tls.to_spec(),
                allowed_routes=allowed_routes,
            )
        )

    @classmethod
    def http(cls, *, name: str, port: int, allowed_routes: GatewaySpecListenersAllowedRoutes | None = None) -> Listener:
        return cls(GatewaySpecListeners(name=name, port=port, protocol="HTTP", allowed_routes=allowed_routes))
