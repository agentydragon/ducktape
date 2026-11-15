"""Endpoint factories for Prometheus Operator's PodMonitor CRD.

Every field the CRD schema itself leaves untyped, and every endpoint shape beyond the one
below (e.g. `relabelings`, `params`), has no factory here -- a caller builds
`PodMonitorSpecPodMetricsEndpoints` directly and passes it into `pod_metrics_endpoints=`,
per cluster/skills/cdk8s_builders/SKILL.md's escape-hatch guidance.
"""

from __future__ import annotations

from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecPodMetricsEndpointsScheme,
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
