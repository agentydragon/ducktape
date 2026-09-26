"""Ergonomic wrapper for Prometheus Operator's `PodMonitor`, mirroring `service_monitor.py`
for the Pod-scraping CRD (a separately `cdk8s_import`-generated dataclass family, even
though structurally near-identical to `ServiceMonitor`'s).

Every field the CRD schema itself leaves untyped, and every endpoint shape beyond the one
below (e.g. `relabelings`, `params`), has no factory here -- a caller builds
`PodMonitorSpecPodMetricsEndpoints` directly and passes it into `pod_metrics_endpoints=`,
per cluster/skills/cdk8s_builders/SKILL.md's escape-hatch guidance.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitor as _PodMonitor,
    PodMonitorSpec,
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsScheme,
    PodMonitorSpecSelector,
)


class Endpoint:
    """`PodMonitorSpecPodMetricsEndpoints`'s real variant shapes this repo uses. Only the
    no-auth shape has a caller today -- no `bearer_token_secret` factory here until one
    does; see `ServiceMonitor.Endpoint` for that shape's counterpart on the sibling CRD.
    """

    @staticmethod
    def plain(
        *,
        port: str,
        path: str = "/metrics",
        scrape_timeout: str | None = None,
        scheme: PodMonitorSpecPodMetricsEndpointsScheme | None = None,
    ) -> PodMonitorSpecPodMetricsEndpoints:
        """No client authentication -- this repo's only case today."""
        return PodMonitorSpecPodMetricsEndpoints(port=port, path=path, scrape_timeout=scrape_timeout, scheme=scheme)


class PodMonitor(_PodMonitor):
    """Prometheus Operator's `PodMonitor`. `selector` is `spec.selector.matchLabels`;
    `pod_metrics_endpoints` is `spec.podMetricsEndpoints`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        selector: dict[str, str],
        pod_metrics_endpoints: Sequence[PodMonitorSpecPodMetricsEndpoints],
    ) -> None:
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=PodMonitorSpec(
                selector=PodMonitorSpecSelector(match_labels=selector),
                pod_metrics_endpoints=list(pod_metrics_endpoints),
            ),
        )
