"""Ergonomic wrapper for Prometheus Operator's `ServiceMonitor`: a class named after the
kind, plus `Endpoint`, grouping the CRD's real alternative endpoint-authentication shapes
this repo uses (no auth; a bearer token via either `bearerTokenSecret` or `authorization`)
the way cdk8s-plus groups `Volume.from_config_map`/`.from_secret`/... under one type.

Every field the CRD schema itself leaves untyped, and every endpoint shape beyond the
ones below (e.g. `relabelings`, `params`, `basicAuth`, `oauth2`), has no factory here --
a caller builds `ServiceMonitorSpecEndpoints` directly and passes it into `endpoints=`,
per cluster/skills/cdk8s_builders/SKILL.md's escape-hatch guidance.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor as _ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsAuthorization,
    ServiceMonitorSpecEndpointsAuthorizationCredentials,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)


class Endpoint:
    """`ServiceMonitorSpecEndpoints`'s real variant shapes this repo uses: no client
    authentication, and a bearer token read from a Secret via either of the schema's two
    mutually-exclusive shapes for that (`bearerTokenSecret`, and its replacement
    `authorization`). The schema also defines `basicAuth`/`oauth2` -- add a factory for
    one the day this repo builds it.
    """

    @staticmethod
    def plain(
        *,
        port: str,
        path: str = "/metrics",
        scrape_timeout: str | None = None,
        scheme: ServiceMonitorSpecEndpointsScheme | None = None,
    ) -> ServiceMonitorSpecEndpoints:
        """No client authentication -- this repo's common case."""
        return ServiceMonitorSpecEndpoints(port=port, path=path, scrape_timeout=scrape_timeout, scheme=scheme)

    @staticmethod
    def bearer_token_secret(
        *, port: str, secret_name: str, key: str, path: str = "/metrics", scrape_timeout: str | None = None
    ) -> ServiceMonitorSpecEndpoints:
        """A bearer token read from `secret_name`'s `key`, in the `ServiceMonitor`'s own
        namespace, via the deprecated `bearerTokenSecret` field."""
        return ServiceMonitorSpecEndpoints(
            port=port,
            path=path,
            scrape_timeout=scrape_timeout,
            bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(name=secret_name, key=key),
        )

    @staticmethod
    def bearer_authorization(
        *, port: str, secret_name: str, key: str, path: str = "/metrics", scrape_timeout: str | None = None
    ) -> ServiceMonitorSpecEndpoints:
        """A bearer token read from `secret_name`'s `key`, in the `ServiceMonitor`'s own
        namespace, via `authorization` -- the CRD schema's newer, more general
        replacement for `bearerTokenSecret` (not wire-identical to it: a different
        `spec.endpoints[]` field, not just a respelling)."""
        return ServiceMonitorSpecEndpoints(
            port=port,
            path=path,
            scrape_timeout=scrape_timeout,
            authorization=ServiceMonitorSpecEndpointsAuthorization(
                type="Bearer",
                credentials=ServiceMonitorSpecEndpointsAuthorizationCredentials(name=secret_name, key=key),
            ),
        )


class ServiceMonitor(_ServiceMonitor):
    """Prometheus Operator's `ServiceMonitor`. Keywords are `ServiceMonitorSpec` fields under
    their own names and types; `None` leaves a field unset, so the operator's own default applies.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        selector: ServiceMonitorSpecSelector,
        endpoints: Sequence[ServiceMonitorSpecEndpoints],
        namespace_selector: ServiceMonitorSpecNamespaceSelector | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ServiceMonitorSpec(
                selector=selector, endpoints=list(endpoints), namespace_selector=namespace_selector
            ),
        )
