"""etcd metrics scoped to the live Talos metrics scrape."""

from grafana_foundation_sdk.builders import timeseries
from grafana_foundation_sdk.builders.common import VizLegendOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir_query

_JOB = 'job="talos-etcd-metrics"'


def _query(metric: str, labels: str = "") -> str:
    extra_labels = f", {labels}" if labels else ""
    return f"{metric}{{{_JOB}{extra_labels}}}"


_RPC_NON_OK = _query("grpc_server_handled_total", 'grpc_type="unary", grpc_code!="OK"')
_RPC_STARTED = _query("grpc_server_started_total", 'grpc_type="unary"')
_WAL_BUCKETS = _query("etcd_disk_wal_fsync_duration_seconds_bucket")
_BACKEND_BUCKETS = _query("etcd_disk_backend_commit_duration_seconds_bucket")


def _panel(
    panel_id: int,
    title: str,
    expression: str,
    legend: str | None = None,
    *,
    unit: str = "short",
    description: str | None = None,
) -> timeseries.Panel:
    x = 0 if panel_id % 2 else 12
    y = ((panel_id - 1) // 2) * 8
    panel = (
        line_panel()
        .id(panel_id)
        .title(title)
        .grid_pos(dashboard.GridPos(h=8, x=x, y=y, w=12))
        .unit(unit)
        .legend(VizLegendOptions().display_mode(common.LegendDisplayMode.LIST).show_legend(True))
        .with_target(mimir_query(expression, legend))
    )
    if description:
        panel.description(description)
    return panel


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("Etcd cluster members")
        .uid("68bafb80-bf37-4a3c-a910-a09b9dc95a86")
        .description(
            'Live etcd member metrics from job="talos-etcd-metrics". Rate panels use 5-minute windows to span multiple 60-second scrapes. '
            "Member gauges and counters retain the instance label; proposal rates are shown per member to avoid presenting replicated consensus work as a single cluster total. "
            "The imported dashboard queried an absent DB-size metric, used an unfiltered process-memory metric, and labeled leader changes as failed proposals; this dashboard uses the metric families and selectors present in the live scrape."
        )
        .tooltip(dashboard.DashboardCursorSync.CROSSHAIR)
        .tags(["etcd", "kubernetes", "cluster"])
        .refresh("1m")
        .time("now-3h", "now")
        .version(1)
        .with_panel(
            _panel(
                1,
                "Member reports a leader",
                f"max by (instance) ({_query('etcd_server_has_leader')})",
                "{{instance}}",
                description="This shows whether each member reports a leader; it does not identify the elected leader.",
            )
        )
        .with_panel(
            _panel(
                2, "Leader changes observed per member", _query("etcd_server_leader_changes_seen_total"), "{{instance}}"
            )
        )
        .with_panel(
            _panel(3, "Failed proposals seen per member", _query("etcd_server_proposals_failed_total"), "{{instance}}")
        )
        .with_panel(
            _panel(
                4,
                "Proposal commit and apply rates per member",
                f"rate({_query('etcd_server_proposals_committed_total')}[5m])",
                "Committed {{instance}}",
                unit="ops",
            ).with_target(
                mimir_query(f"rate({_query('etcd_server_proposals_applied_total')}[5m])", "Applied {{instance}}", "B")
            )
        )
        .with_panel(
            _panel(
                5,
                "Proposals pending per member",
                _query("etcd_server_proposals_pending"),
                "{{instance}}",
                description="Zero pending proposals is the expected steady state; the metric is emitted per member.",
            )
        )
        .with_panel(
            _panel(
                6, "Database size per member", _query("etcd_mvcc_db_total_size_in_bytes"), "{{instance}}", unit="bytes"
            )
        )
        .with_panel(
            _panel(
                7,
                "etcd process resident memory per member",
                _query("process_resident_memory_bytes"),
                "{{instance}}",
                unit="bytes",
            )
        )
        .with_panel(
            _panel(
                8,
                "WAL fsync and backend commit p99 duration",
                f"histogram_quantile(0.99, sum by (instance, le) (rate({_WAL_BUCKETS}[5m])))",
                "WAL fsync {{instance}}",
                unit="s",
            ).with_target(
                mimir_query(
                    f"histogram_quantile(0.99, sum by (instance, le) (rate({_BACKEND_BUCKETS}[5m])))",
                    "Backend commit {{instance}}",
                    "B",
                )
            )
        )
        .with_panel(
            _panel(
                9,
                "WAL fsync and backend commit operations per second",
                f"rate({_query('etcd_disk_wal_fsync_duration_seconds_count')}[5m])",
                "WAL fsync {{instance}}",
                unit="ops",
            ).with_target(
                mimir_query(
                    f"rate({_query('etcd_disk_backend_commit_duration_seconds_count')}[5m])",
                    "Backend commit {{instance}}",
                    "B",
                )
            )
        )
        .with_panel(
            _panel(
                10,
                "Unary RPC requests per second",
                f"sum(rate({_RPC_STARTED}[5m]))",
                "Unary requests",
                unit="ops",
                description="Aggregate unary requests recorded across the three etcd member endpoints.",
            )
        )
        .with_panel(
            _panel(
                11,
                "Non-OK unary responses per second by code",
                f"sum by (grpc_code) (rate({_RPC_NON_OK}[5m]))",
                "{{grpc_code}}",
                unit="ops",
                description="Non-OK gRPC codes can include expected client cancellations and deadline expirations, so they do not all indicate an etcd server fault. The code label distinguishes the observed response status.",
            )
        )
        .with_panel(
            _panel(
                12,
                "Active Watch and Lease streams",
                'sum(grpc_server_started_total{grpc_service="etcdserverpb.Watch", grpc_type="bidi_stream", job="talos-etcd-metrics"}) - sum(grpc_server_handled_total{grpc_service="etcdserverpb.Watch", grpc_type="bidi_stream", job="talos-etcd-metrics"})',
                "Watch",
                unit="short",
            ).with_target(
                mimir_query(
                    'sum(grpc_server_started_total{grpc_service="etcdserverpb.Lease", grpc_type="bidi_stream", job="talos-etcd-metrics"}) - sum(grpc_server_handled_total{grpc_service="etcdserverpb.Lease", grpc_type="bidi_stream", job="talos-etcd-metrics"})',
                    "Lease",
                    "B",
                )
            )
        )
        .with_panel(
            _panel(
                13,
                "Client traffic received and sent per member",
                f"rate({_query('etcd_network_client_grpc_received_bytes_total')}[5m])",
                "Received {{instance}}",
                unit="Bps",
            ).with_target(
                mimir_query(
                    f"rate({_query('etcd_network_client_grpc_sent_bytes_total')}[5m])", "Sent {{instance}}", "B"
                )
            )
        )
        .with_panel(
            _panel(
                14,
                "Peer traffic received and sent per member",
                f"sum by (instance) (rate({_query('etcd_network_peer_received_bytes_total')}[5m]))",
                "Received {{instance}}",
                unit="Bps",
            ).with_target(
                mimir_query(
                    f"sum by (instance) (rate({_query('etcd_network_peer_sent_bytes_total')}[5m]))",
                    "Sent {{instance}}",
                    "B",
                )
            )
        )
        .with_panel(
            _panel(
                15,
                "Average snapshot save duration per member",
                f"rate({_query('etcd_debugging_snap_save_total_duration_seconds_sum')}[5m]) / rate({_query('etcd_debugging_snap_save_total_duration_seconds_count')}[5m])",
                "{{instance}}",
                unit="s",
            )
        )
        .with_panel(
            _panel(
                16,
                "Leader changes during the last 24 hours",
                f"increase({_query('etcd_server_leader_changes_seen_total')}[1d])",
                "{{instance}}",
                description="This is a counter increase over 24 hours per member. A value of zero is expected when leadership is stable.",
            )
        )
        .build()
    )
    result.schema_version = 39
    return result
