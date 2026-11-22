"""TabbyAPI inference throughput, request activity and prefix cache."""

from grafana_foundation_sdk.builders import stat, timeseries
from grafana_foundation_sdk.builders.common import ReduceDataOptions, VizLegendOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard, ThresholdsConfig
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir, mimir_query

_TARGET = '{namespace="ollama",job="ollama/tabbyapi"}'
_GPU_NODE = 'max by (namespace, pod) (kube_pod_info{namespace="dcgm-exporter",node="wyrm2"})'


def _gpu_memory(metric: str) -> str:
    return (
        f'max by (UUID, gpu) (DCGM_FI_DEV_FB_{metric}{{namespace="dcgm-exporter"}} '
        f"* on (namespace, pod) group_left() {_GPU_NODE}) * 1048576"
    )


def _panel(panel_id: int, title: str, x: int, y: int, unit: str, description: str) -> timeseries.Panel:
    return (
        line_panel()
        .id(panel_id)
        .title(title)
        .description(description)
        .grid_pos(dashboard.GridPos(h=8, w=12, x=x, y=y))
        .unit(unit)
        .min(0)
        .legend(
            VizLegendOptions().display_mode(common.LegendDisplayMode.TABLE).show_legend(True).calcs(["lastNotNull"])
        )
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("TabbyAPI inference")
        .uid("tabbyapi-inference")
        .description("Inference throughput, requests, cache and GPU memory for TabbyAPI on wyrm2.")
        .tags(["inference", "tabbyapi", "gpu"])
        .refresh("1m")
        .time("now-6h", "now")
        .version(1)
        .with_panel(
            stat.Panel()
            .id(1)
            .title("Metrics scrape")
            .description("Whether Alloy can scrape TabbyAPI. Zero also includes a missing target.")
            .datasource(mimir())
            .grid_pos(dashboard.GridPos(h=4, w=24, x=0, y=0))
            .unit("short")
            .thresholds(
                ThresholdsConfig().steps(
                    [dashboard.Threshold(color="red"), dashboard.Threshold(value=1, color="green")]
                )
            )
            .reduce_options(ReduceDataOptions().calcs(["lastNotNull"]).fields("").values(False))
            .with_target(mimir_query(f"max(up{_TARGET}) or vector(0)", "").instant())
        )
        .with_panel(
            _panel(
                2,
                "Prefill speed",
                0,
                4,
                "suffix: tok/s",
                "Prompt tokens per active prefill second since the previous scrape. Updated when a request finishes; zero can mean no request finished in the interval. Multiple scrapers share the same gauge window.",
            ).with_target(mimir_query(f"tabbyapi:prompt_tokens_seconds{_TARGET}", "Prefill"))
        )
        .with_panel(
            _panel(
                3,
                "Generation speed",
                12,
                4,
                "suffix: tok/s",
                "Generated tokens per active generation second since the previous scrape. Zero can mean no generation in the interval. Multiple scrapers share the same gauge window.",
            ).with_target(mimir_query(f"tabbyapi:predicted_tokens_seconds{_TARGET}", "Generation"))
        )
        .with_panel(
            _panel(
                4,
                "Tokens processed per wall second",
                0,
                12,
                "suffix: tok/s",
                "Five-minute token-counter rate, including idle time. Prefill excludes cache hits; counters can advance while a request is running.",
            )
            .with_target(mimir_query(f"rate(tabbyapi:prompt_tokens_total{_TARGET}[5m])", "Prefill"))
            .with_target(mimir_query(f"rate(tabbyapi:tokens_predicted_total{_TARGET}[5m])", "Generation", "B"))
        )
        .with_panel(
            _panel(
                5, "Requests", 12, 12, "short", "TabbyAPI jobs currently processing or deferred (queued or waiting)."
            )
            .with_target(mimir_query(f"tabbyapi:requests_processing{_TARGET}", "Processing"))
            .with_target(mimir_query(f"tabbyapi:requests_deferred{_TARGET}", "Deferred", "B"))
        )
        .with_panel(
            _panel(
                6,
                "KV cache tokens",
                0,
                20,
                "short",
                "Current GPU KV cache use and capacity, plus reusable tokens in the KV cache, system memory and disk. Tiers describe different storage; they are not request counts.",
            )
            .grid_pos(dashboard.GridPos(h=13, w=12, x=0, y=20))
            .with_target(mimir_query(f"tabbyapi:kv_cache_tokens{_TARGET}", "In use"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_max_tokens{_TARGET}", "Capacity", "B"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_reusable_tokens{_TARGET}", "Reusable KV", "C"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_sysmem_tokens{_TARGET}", "Reusable RAM", "D"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_disk_tokens{_TARGET}", "Reusable disk", "E"))
        )
        .with_panel(
            _panel(
                7,
                "Prefix cache hit rates",
                12,
                20,
                "percentunit",
                "Fraction of prompt pages served from cache since model load; RAM and disk are individual tiers. Zero is normal before cache hits.",
            )
            .grid_pos(dashboard.GridPos(h=11, w=12, x=12, y=20))
            .max(1)
            .with_target(mimir_query(f"tabbyapi:kv_cache_hit_rate{_TARGET}", "Total"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_sysmem_hit_rate{_TARGET}", "RAM", "B"))
            .with_target(mimir_query(f"tabbyapi:kv_cache_disk_hit_rate{_TARGET}", "Disk", "C"))
        )
        .with_panel(
            _panel(
                8,
                "GPU memory",
                0,
                33,
                "bytes",
                "Device-wide framebuffer memory on wyrm2, including desktop and other processes. DCGM reports MiB, converted to bytes; it does not attribute usage to TabbyAPI.",
            )
            .grid_pos(dashboard.GridPos(h=10, w=24, x=0, y=33))
            .with_target(mimir_query(_gpu_memory("USED"), "GPU {{gpu}} used"))
            .with_target(mimir_query(_gpu_memory("FREE"), "GPU {{gpu}} free", "B"))
        )
        .build()
    )
    result.schema_version = 39
    return result
