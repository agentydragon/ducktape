"""Ergonomic wrapper for Prometheus Operator's `ServiceMonitor`: a class named after the
kind, plus `Endpoint`, grouping the CRD's real alternative endpoint-authentication shapes
this repo uses (no auth; `bearerTokenSecret`) the way cdk8s-plus groups
`Volume.from_config_map`/`.from_secret`/... under one type.

Every field the CRD schema itself leaves untyped, and every endpoint shape beyond the two
below (e.g. `relabelings`, `params`), has no factory here -- a caller builds
`ServiceMonitorSpecEndpoints` directly and passes it into `endpoints=`, per
cluster/skills/cdk8s_builders/SKILL.md's escape-hatch guidance.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from prometheus_operator_crds.com.coreos.monitoring import (
    ServiceMonitor as _ServiceMonitor,
    ServiceMonitorSpec,
    ServiceMonitorSpecEndpoints,
    ServiceMonitorSpecEndpointsBearerTokenSecret,
    ServiceMonitorSpecEndpointsScheme,
    ServiceMonitorSpecNamespaceSelector,
    ServiceMonitorSpecSelector,
)


class Endpoint:
    """`ServiceMonitorSpecEndpoints`'s real variant shapes this repo uses: no client
    authentication, and a bearer token read from a Secret. The schema also defines
    `authorization`/`basicAuth`/`oauth2` -- add a factory for one the day this repo builds
    it.
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
        namespace."""
        return ServiceMonitorSpecEndpoints(
            port=port,
            path=path,
            scrape_timeout=scrape_timeout,
            bearer_token_secret=ServiceMonitorSpecEndpointsBearerTokenSecret(name=secret_name, key=key),
        )


class ServiceMonitor(_ServiceMonitor):
    """Prometheus Operator's `ServiceMonitor`. `selector` is `spec.selector.matchLabels`;
    `endpoints` is `spec.endpoints`. `namespace_selector`, when given, is
    `spec.namespaceSelector.matchNames` -- by default Prometheus discovers the scraped
    `Service` only in the `ServiceMonitor`'s own namespace, and this widens that to the
    named namespaces.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        selector: dict[str, str],
        endpoints: Sequence[ServiceMonitorSpecEndpoints],
        namespace_selector: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=ServiceMonitorSpec(
                selector=ServiceMonitorSpecSelector(match_labels=selector),
                endpoints=list(endpoints),
                namespace_selector=(
                    ServiceMonitorSpecNamespaceSelector(match_names=list(namespace_selector))
                    if namespace_selector is not None
                    else None
                ),
            ),
        )
