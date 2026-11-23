"""Flux readiness and reconciliation dashboard using typed SDK builders."""

from grafana_foundation_sdk.builders import stat, timeseries
from grafana_foundation_sdk.builders.common import ReduceDataOptions, VizLegendOptions, VizTooltipOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard, FieldColor, ThresholdsConfig
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir, mimir_multi_query_variable, mimir_query

_NAMESPACE_QUERY = "label_values(kube_customresource_flux_kustomization_ready, namespace)"
_RESOURCE_KINDS = ("Kustomization", "HelmRelease", "GitRepository")


def _readiness_stat(zero_color: str = "red", nonzero_color: str = "green") -> stat.Panel:
    return (
        stat.Panel()
        .datasource(mimir())
        .unit("short")
        .color_scheme(FieldColor().mode(dashboard.FieldColorModeId.THRESHOLDS))
        .thresholds(
            ThresholdsConfig().steps(
                [dashboard.Threshold(color=zero_color), dashboard.Threshold(color=nonzero_color, value=1)]
            )
        )
        .reduce_options(ReduceDataOptions().calcs(["lastNotNull"]).fields("").values(False))
    )


def _history_panel() -> timeseries.Panel:
    return line_panel().span_nulls(True).legend(VizLegendOptions().show_legend(True))


def _resource_readiness(id: int, kind: str, *, ready: bool = True) -> stat.Panel:
    plural = "GitRepositories" if kind == "GitRepository" else f"{kind}s"
    status = 'status="True"' if ready else 'status!="True"'
    expression = f'kube_customresource_flux_{kind.lower()}_ready{{namespace=~"$namespace",{status}}}'
    # These are one-hot 0/1 gauges, so sum counts matching conditions and preserves an all-zero result.
    expression = f"sum({expression})"
    return (
        (_readiness_stat() if ready else _readiness_stat("green", "red"))
        .id(id)
        .title(f"{'Ready' if ready else 'Not-ready'} {plural}")
        .description(
            f"{plural} whose Ready condition is True."
            if ready
            else f"{plural} whose current Ready condition is False or Unknown."
        )
        .grid_pos(dashboard.GridPos(h=5, w=6, x=(id - 1) * 6))
        .with_target(mimir_query(expression, "ready" if ready else "not ready").instant())
    )


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("Flux cluster")
        .uid("eM7_f4V7z")
        .description("Flux readiness and reconciliation metrics from kube-state-metrics and the Flux controllers.")
        .tags(["flux", "kubernetes"])
        .version(1)
        .refresh("1m")
        .time("now-6h", "now")
        .with_variable(mimir_multi_query_variable("namespace", "Namespace", _NAMESPACE_QUERY))
        .with_panel(_resource_readiness(1, "Kustomization"))
        .with_panel(_resource_readiness(2, "Kustomization", ready=False))
        .with_panel(_resource_readiness(3, "HelmRelease"))
        .with_panel(_resource_readiness(4, "GitRepository"))
        .with_panel(
            _history_panel()
            .id(5)
            .title("Reconciliation duration by resource kind")
            .grid_pos(dashboard.GridPos(y=5))
            .unit("s")
            .line_interpolation(common.LineInterpolation.SMOOTH)
            .with_target(
                mimir_query(
                    "sum(rate(gotk_reconcile_duration_seconds_sum[5m])) by (kind) / sum(rate(gotk_reconcile_duration_seconds_count[5m])) by (kind)",
                    "{{kind}}",
                )
            )
        )
        .with_panel(
            _history_panel()
            .id(6)
            .title("Reconciliations per second")
            .grid_pos(dashboard.GridPos(x=12, y=5))
            .unit("ops")
            .with_target(mimir_query("sum(rate(gotk_reconcile_duration_seconds_count[5m])) by (kind)", "{{kind}}"))
        )
        .with_panel(
            _history_panel()
            .id(7)
            .title("Not-ready resources by namespace")
            .grid_pos(dashboard.GridPos(h=8, y=14))
            .unit("short")
            .targets(
                [
                    mimir_query(
                        f'sum by (namespace) (kube_customresource_flux_{kind.lower()}_ready{{namespace=~"$namespace",status!="True"}})',
                        f"{kind} {{{{namespace}}}}",
                        ref_id=chr(ord("A") + index),
                    )
                    for index, kind in enumerate(_RESOURCE_KINDS)
                ]
            )
        )
        .with_panel(
            _history_panel()
            .id(8)
            .title("GitRepository artifact size")
            .grid_pos(dashboard.GridPos(h=8, x=12, y=14))
            .unit("decbytes")
            .tooltip(VizTooltipOptions().mode(common.TooltipDisplayMode.SINGLE))
            .with_target(
                mimir_query(
                    'kube_customresource_flux_gitrepository_artifact_size_bytes{namespace=~"$namespace"}',
                    "{{namespace}}/{{name}}",
                )
            )
        )
        .build()
    )
    result.schema_version = 39
    return result
