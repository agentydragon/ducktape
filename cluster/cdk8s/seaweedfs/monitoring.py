"""SeaweedFS replication and S3 gateway PrometheusRules.

The seaweedfs-operator generates and owns ServiceMonitors for the Seaweed CR's components
(including its internal S3 gateway) from `metricsPort` fields. Change those ports in
`cluster.py`; the operator names them `<component>-metrics` (e.g. `master-metrics`). The
independent public S3 gateway and its ServiceMonitor belong to `public_s3.py`.
"""

from __future__ import annotations

from textwrap import dedent

from cdk8s import ApiObjectMetadata, App, Chart

from cluster.cdk8s.providers.prometheus_operator.prometheus_rule import PrometheusRule, Rule, group
from cluster.cdk8s.seaweedfs import cluster, namespace, public_s3

NAME = "seaweedfs-monitoring"
_S3_GATEWAY_SERVICES = (f"{cluster.NAME}-s3", public_s3.NAME)
_S3_GATEWAY_SERVICE_MATCHER = "|".join(_S3_GATEWAY_SERVICES)


_S3_GATEWAY_REQUEST_ERRORS = [
    Rule.alert(
        "SeaweedS3RequestErrorsHigh",
        dedent(
            f"""\
            (
              (
                sum by (namespace, service, type) (
                  increase(
                    SeaweedFS_s3_request_total{{
                      namespace="{namespace.NAME}",
                      service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                      code=~"5.."
                    }}[5m]
                  )
                )
                /
                on (namespace, service, type)
                sum by (namespace, service, type) (
                  increase(
                    SeaweedFS_s3_request_total{{
                      namespace="{namespace.NAME}",
                      service=~"{_S3_GATEWAY_SERVICE_MATCHER}"
                    }}[5m]
                  )
                )
              ) >= 0.05
              and on (namespace, service, type)
              sum by (namespace, service, type) (
                increase(
                  SeaweedFS_s3_request_total{{
                    namespace="{namespace.NAME}",
                    service=~"{_S3_GATEWAY_SERVICE_MATCHER}"
                  }}[5m]
                )
              ) >= 20
            )
            or
            (
              sum by (namespace, service, type) (
                increase(
                  SeaweedFS_s3_request_total{{
                    namespace="{namespace.NAME}",
                    service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                    code=~"5.."
                  }}[15m]
                )
              ) >= 4
              and on (namespace, service, type)
              sum by (namespace, service, type) (
                increase(
                  SeaweedFS_s3_request_total{{
                    namespace="{namespace.NAME}",
                    service=~"{_S3_GATEWAY_SERVICE_MATCHER}"
                  }}[15m]
                )
              ) < 60
            )
            """
        ),
        for_="10m",
        labels={"severity": "warning"},
        summary="SeaweedFS S3 {{ $labels.service }} {{ $labels.type }} requests are returning 5xx",
        description=(
            "For {{ $labels.service }} {{ $labels.type }}, either at least 5% of at least 20 requests in each trailing "
            "5-minute window returned 5xx for 10 minutes, or at least 4 requests returned 5xx in the trailing 15 "
            "minutes while fewer than 60 requests of that type were made. Either condition must remain true for 10 "
            "minutes. A compact burst of four failures can satisfy the low-volume branch while those errors remain "
            "inside the rolling 15-minute window; failures spread near the window boundary may not. Request types are "
            "evaluated separately, so read traffic cannot dilute write errors. Expected S3 4xx responses and client "
            "cancellations (499) are excluded."
        ),
    )
]

# Full request duration includes payload transfer time. Use GET time-to-first-byte so a
# large healthy download does not look like a slow gateway; keep full-duration thresholds
# for the size-independent metadata operations. Each p95 needs at least 20 requests in its
# trailing 5m window, and the threshold must persist for 15m. These are warning tripwires,
# not SLOs; payload-bearing PUT/POST/COPY requests are excluded from this latency alert.
_S3_GATEWAY_LATENCY = [
    Rule.alert(
        "SeaweedS3GatewayLatencyHigh",
        dedent(
            f"""\
            (
              (
                histogram_quantile(
                  0.95,
                  sum by (namespace, service, type, le) (
                    rate(
                      SeaweedFS_s3_time_to_first_byte_millisecond_bucket{{
                        namespace="{namespace.NAME}",
                        service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                        type="GET"
                      }}[5m]
                    )
                  )
                ) / 1000 > 30
                and on (namespace, service, type)
                sum by (namespace, service, type) (
                  increase(
                    SeaweedFS_s3_request_total{{
                      namespace="{namespace.NAME}",
                      service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                      type="GET"
                    }}[5m]
                  )
                ) >= 20
              )
              or
              (
                histogram_quantile(
                  0.95,
                  sum by (namespace, service, type, le) (
                    rate(
                      SeaweedFS_s3_request_seconds_bucket{{
                        namespace="{namespace.NAME}",
                        service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                        type=~"DELETE|LIST"
                      }}[5m]
                    )
                  )
                ) > 5
                and on (namespace, service, type)
                sum by (namespace, service, type) (
                  increase(
                    SeaweedFS_s3_request_total{{
                      namespace="{namespace.NAME}",
                      service=~"{_S3_GATEWAY_SERVICE_MATCHER}",
                      type=~"DELETE|LIST"
                    }}[5m]
                  )
                ) >= 20
              )
            )
            """
        ),
        for_="15m",
        labels={"severity": "warning"},
        summary="SeaweedFS S3 {{ $labels.service }} {{ $labels.type }} latency is high",
        description=(
            "The 95th percentile of GET time-to-first-byte exceeded 30s, or the 95th percentile of DELETE/LIST request "
            "duration exceeded 5s, with at least 20 requests of that type in each trailing 5-minute window for 15 "
            "minutes. GET uses time-to-first-byte because download size affects total duration. PUT, POST, and COPY "
            "full duration is excluded because payload size can explain it."
        ),
    )
]

_S3_GATEWAY_TARGETS_MISSING = [
    Rule.alert(
        "SeaweedS3GatewayTargetMissing",
        f'absent_over_time(up{{namespace="{namespace.NAME}",service="{service}"}}[10m])',
        for_="5m",
        labels={"severity": "warning"},
        summary="SeaweedFS S3 metrics target {{ $labels.service }} is missing",
        description=(
            "No `up` samples have been discovered for the expected {{ $labels.service }} metrics service in "
            f"{namespace.NAME} for 15 minutes. This indicates missing target discovery or ServiceMonitor wiring. "
            "A discovered target whose scrape returns `up=0` is handled by TargetDown."
        ),
    )
    for service in _S3_GATEWAY_SERVICES
]

_S3_GATEWAY_TARGET_COUNT_LOW = [
    Rule.alert(
        "SeaweedS3GatewayTargetCountLow",
        dedent(
            f"""\
            count by (namespace, service) (
              max by (namespace, service, pod) (
                up{{namespace="{namespace.NAME}",service="{service}"}}
              )
            )
            < on (namespace, service)
            label_replace(
              max by (namespace, deployment) (
                kube_deployment_spec_replicas{{namespace="{namespace.NAME}",deployment="{service}"}}
              ),
              "service", "$1", "deployment", "(.*)"
            )
            """
        ),
        for_="10m",
        labels={"severity": "warning"},
        summary="SeaweedFS S3 metrics targets for {{ $labels.service }} are below desired replicas",
        description=(
            "The number of discovered `up` targets for {{ $labels.service }} is below the Kubernetes Deployment's "
            "desired replica count for 10 minutes. This detects a missing pod target even while another replica is "
            "healthy. A target that is discovered but returns `up=0` is handled by TargetDown."
        ),
    )
    for service in _S3_GATEWAY_SERVICES
]

_S3_GATEWAY_METRICS_MISSING = [
    Rule.alert(
        "SeaweedS3GatewayMetricsMissing",
        dedent(
            f"""\
            (
              up{{namespace="{namespace.NAME}",service="{service}"}} == 1
            )
            unless on (namespace, service, pod)
            max by (namespace, service, pod) (
              present_over_time(
                SeaweedFS_s3_in_flight_requests{{namespace="{namespace.NAME}",service="{service}"}}[10m]
              )
            )
            """
        ),
        for_="5m",
        labels={"severity": "warning"},
        summary="SeaweedFS S3 gauge metrics are missing from {{ $labels.service }} pod {{ $labels.pod }}",
        description=(
            "This pod has had successful metric scrapes but the always-exported "
            "SeaweedFS_s3_in_flight_requests gauge has been absent for 10 minutes. The expression is per pod, so a "
            "healthy sibling does not mask a malformed exporter. If the target itself is absent, "
            "SeaweedS3GatewayTargetCountLow or SeaweedS3GatewayTargetMissing covers it; if the scrape returns `up=0`, "
            "TargetDown covers it."
        ),
    )
    for service in _S3_GATEWAY_SERVICES
]


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    PrometheusRule(
        chart,
        "seaweedfs-replication",
        metadata=ApiObjectMetadata(name="replication", namespace=namespace.NAME),
        groups=[
            group(
                "seaweedfs-replication",
                [
                    # SeaweedFS master does not self-heal under-replicated volumes. The operator's
                    # replication-repair AdminScript attempts hourly, copy-only repair; this alert
                    # still exposes stalled jobs, missing destination slots, and any excess or
                    # misplaced replicas, which the script deliberately does not delete.
                    #
                    # `SeaweedFS_master_replica_placement_mismatch` is a leader-only, per-volume
                    # gauge (labels: collection, id) = 1 when a volume's replica count/placement
                    # does not match its target (under- or over-replicated), else 0. Only the
                    # leader emits it, so `sum()` counts mismatched volumes cluster-wide.
                    Rule.alert(
                        "SeaweedFSReplicaPlacementMismatch",
                        "sum(SeaweedFS_master_replica_placement_mismatch) > 0",
                        for_="15m",
                        labels={"severity": "warning"},
                        summary="SeaweedFS has {{ $value }} volume(s) off their replica count",
                        description=(
                            "{{ $value }} SeaweedFS volume(s) have not matched their target replica "
                            "placement for 15m (replication 001 = 2 copies). The replication-repair "
                            "AdminScript attempts copy-only repair hourly; check its CronJob/Job status and "
                            "available volume slots if the mismatch persists. It never trims excess or "
                            "misplaced replicas, so review those deliberately. Suspend the AdminScript and "
                            "wait for any active Job before planned volume-server maintenance. For an "
                            "immediate manual repair, inspect `volume.list` and run "
                            "`volume.fix.replication -apply -doDelete=false` under `lock` from a master pod "
                            "(`weed shell` reads commands on stdin)."
                        ),
                    )
                ],
            )
        ],
    )
    PrometheusRule(
        chart,
        "seaweedfs-s3-gateway-alerts",
        metadata=ApiObjectMetadata(name="s3-gateway-alerts", namespace=namespace.NAME),
        groups=[
            group("seaweedfs-s3-gateway", _S3_GATEWAY_REQUEST_ERRORS),
            group("seaweedfs-s3-gateway-latency", _S3_GATEWAY_LATENCY),
            group(
                "seaweedfs-s3-gateway-metrics",
                _S3_GATEWAY_TARGETS_MISSING + _S3_GATEWAY_TARGET_COUNT_LOW + _S3_GATEWAY_METRICS_MISSING,
            ),
        ],
    )
    return chart
