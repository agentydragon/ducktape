"""Current Cilium agent metrics verified against the live Mimir scrape."""

from grafana_foundation_sdk.builders.dashboard import Dashboard
from grafana_foundation_sdk.models import dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import (
    mimir_multi_query_variable,
    mimir_query,
    mimir_timeseries_panel,
)

_JOB = 'job="cilium-agent", pod=~"$pod"'


def _query(metric: str, labels: str = "") -> str:
    extra_labels = f", {labels}" if labels else ""
    return f"{metric}{{{_JOB}{extra_labels}}}"


_CONNTRACK = _query("cilium_datapath_conntrack_gc_entries", 'status=~"alive|deleted", family="ipv4", protocol="TCP"')
_DROPS = _query("cilium_drop_count_total", 'direction=~"INGRESS|EGRESS"')
_DROP_BYTES = _query("cilium_drop_bytes_total", 'direction=~"INGRESS|EGRESS"')
_REGEN_BUCKETS = _query("cilium_endpoint_regeneration_time_stats_seconds_bucket", 'scope!="total"')


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("Cilium agent metrics")
        .uid("vtuWtdumz")
        .description(
            'Operational metrics exported by the live Cilium agent scrape (job="cilium-agent"). '
            "This is a focused dashboard for the metrics currently present in Mimir, not a port of every panel from the imported Cilium v1.12 dashboard. "
            "The upstream BPF syscall-duration, KVStore, detailed L7-policy, policy-trigger, service-event, and IPv6 conntrack panels had no matching series in the observed Cilium 1.19.8 scrape. "
            "Missing series are omitted rather than shown as zero; in particular, absent IPv6 conntrack series do not imply no IPv6 traffic. "
            "Rate panels use 5-minute windows to span multiple 60-second scrapes. The Pod selector filters every panel and defaults to All."
        )
        .tooltip(dashboard.DashboardCursorSync.CROSSHAIR)
        .tags(["cilium", "network", "kubernetes"])
        .refresh("1m")
        .time("now-3h", "now")
        .version(1)
        .with_variable(
            mimir_multi_query_variable(
                "pod", "Pod", 'label_values(cilium_process_cpu_seconds_total{job="cilium-agent"}, pod)'
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                1,
                "Agent CPU per pod",
                f"avg(irate({_query('cilium_process_cpu_seconds_total')}[5m])) by (pod) * 100",
                "{{pod}}",
                unit="percent",
                description="CPU use as a percentage of one CPU core, by Cilium agent pod.",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                2,
                "Agent resident memory per pod",
                f"max by (pod) ({_query('cilium_process_resident_memory_bytes')})",
                "{{pod}}",
                unit="bytes",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                3, "Agent open file descriptors", f"max by (pod) ({_query('cilium_process_open_fds')})", "{{pod}}"
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                4,
                "BPF map and program virtual memory per pod",
                f"max by (pod) ({_query('cilium_bpf_maps_virtual_memory_max_bytes')} + {_query('cilium_bpf_progs_virtual_memory_max_bytes')})",
                "{{pod}}",
                unit="bytes",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                5,
                "Maximum BPF map pressure per pod",
                f"max by (pod) ({_query('cilium_bpf_map_pressure')})",
                "{{pod}}",
                unit="percentunit",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                6,
                "Top BPF map operation rates",
                f"topk(5, avg(rate({_query('cilium_bpf_map_ops_total')}[5m])) by (pod, map_name, operation))",
                "{{pod}} / {{map_name}} / {{operation}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                7,
                "Forwarded packets per second",
                f"sum(rate({_query('cilium_forward_count_total')}[5m])) by (pod, direction)",
                "{{pod}} / {{direction}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                8,
                "Forwarded network throughput",
                f"sum(rate({_query('cilium_forward_bytes_total')}[5m])) by (pod, direction) * 8",
                "{{pod}} / {{direction}}",
                unit="bps",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                9,
                "IPv4 TCP conntrack GC entries per pod and state",
                f"max by (pod, status) ({_CONNTRACK})",
                "{{pod}} / {{status}}",
                description="Per-agent IPv4 TCP entries observed by Cilium conntrack garbage collection, split into alive and deleted states. The currently scraped metric set has no IPv6 conntrack series; absence does not imply no IPv6 traffic.",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                10,
                "Allocated addresses by pod and family",
                f"sum({_query('cilium_ip_addresses')}) by (pod, family)",
                "{{pod}} / {{family}}",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                11,
                "Dropped packets per second by direction and reason",
                f"sum(rate({_DROPS}[5m])) by (direction, reason)",
                "{{direction}} / {{reason}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                12,
                "Dropped traffic by direction and reason",
                f"sum(rate({_DROP_BYTES}[5m])) by (direction, reason) * 8",
                "{{direction}} / {{reason}}",
                unit="bps",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                13,
                "Endpoint regeneration duration p90 and p99",
                f"histogram_quantile(0.90, sum by (scope, le) (rate({_REGEN_BUCKETS}[5m])))",
                "p90 {{scope}}",
                unit="s",
                description="Histogram buckets are pooled across selected agents before calculating quantiles. With Pod set to All, these are cluster-wide endpoint regeneration percentiles.",
            ).with_target(
                mimir_query(
                    f"histogram_quantile(0.99, sum by (scope, le) (rate({_REGEN_BUCKETS}[5m])))", "p99 {{scope}}", "B"
                )
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                14,
                "Controller runs per second",
                f"sum(rate({_query('cilium_controllers_runs_total')}[5m])) by (pod)",
                "{{pod}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                15,
                "Controllers currently failing",
                f"sum({_query('cilium_controllers_failing')}) by (pod)",
                "{{pod}}",
                description="Zero is expected when every Cilium controller is healthy; this gauge is present in the scrape.",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                16,
                "Cilium API request latency",
                f"avg(rate({_query('cilium_agent_api_process_time_seconds_sum')}[5m]) / rate({_query('cilium_agent_api_process_time_seconds_count')}[5m])) by (pod, method, path)",
                "{{pod}} {{method}} {{path}}",
                unit="s",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                17,
                "Cilium API request rate",
                f"avg(rate({_query('cilium_agent_api_process_time_seconds_count')}[5m])) by (pod, method, path)",
                "{{pod}} {{method}} {{path}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                18,
                "Kubernetes API client latency",
                f"avg(rate({_query('cilium_k8s_client_api_latency_time_seconds_sum')}[5m]) / rate({_query('cilium_k8s_client_api_latency_time_seconds_count')}[5m])) by (pod, method, path)",
                "{{pod}} {{method}} {{path}}",
                unit="s",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                19,
                "Endpoint regeneration rate by outcome",
                f"sum(rate({_query('cilium_endpoint_regenerations_total')}[5m])) by (outcome)",
                "{{outcome}}",
                unit="ops",
            )
        )
        .with_panel(
            mimir_timeseries_panel(
                20,
                "Endpoint policy enforcement status",
                f"sum({_query('cilium_policy_endpoint_enforcement_status')}) by (enforcement)",
                "{{enforcement}}",
            )
        )
        .build()
    )
    result.schema_version = 39
    return result
