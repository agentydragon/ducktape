"""Flux readiness and reconciliation dashboard using typed SDK builders."""

from grafana_foundation_sdk.builders import stat, timeseries
from grafana_foundation_sdk.builders.common import ReduceDataOptions, VizLegendOptions, VizTooltipOptions
from grafana_foundation_sdk.builders.dashboard import Dashboard, FieldColor, QueryVariable, ThresholdsConfig
from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import line_panel, mimir, mimir_query

_NAMESPACE_QUERY = "label_values(kube_customresource_flux_kustomization_ready, namespace)"


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


def build_dashboard() -> dashboard.Dashboard:
    result = (
        Dashboard("Flux cluster")
        .uid("eM7_f4V7z")
        .description("Flux readiness and reconciliation metrics from kube-state-metrics and the Flux controllers.")
        .tags(["flux", "kubernetes"])
        .version(1)
        .refresh("1m")
        .time("now-6h", "now")
        .with_variable(
            QueryVariable("namespace")
            .label("Namespace")
            .datasource(mimir())
            .definition(_NAMESPACE_QUERY)
            .query(_NAMESPACE_QUERY)
            .include_all(True)
            .multi(True)
            .all_value(".*")
            .refresh(dashboard.VariableRefresh.ON_DASHBOARD_LOAD)
            .current(dashboard.VariableOption(text="All", value=["$__all"]))
        )
        .with_panel(
            _readiness_stat()
            .id(1)
            .title("Ready Kustomizations")
            .description("Kustomizations whose Ready condition is True.")
            .grid_pos(dashboard.GridPos(h=5, w=6, x=0, y=0))
            .with_target(
                mimir_query(
                    'count(kube_customresource_flux_kustomization_ready{namespace=~"$namespace",status="True"})',
                    "ready",
                ).instant()
            )
        )
        .with_panel(
            _readiness_stat("green", "red")
            .id(2)
            .title("Not-ready Kustomizations")
            .description("Kustomizations whose current Ready condition is False or Unknown.")
            .grid_pos(dashboard.GridPos(h=5, w=6, x=6, y=0))
            .with_target(
                mimir_query(
                    'sum(kube_customresource_flux_kustomization_ready{namespace=~"$namespace",status!="True"})',
                    "not ready",
                ).instant()
            )
        )
        .with_panel(
            _readiness_stat()
            .id(3)
            .title("Ready HelmReleases")
            .description("HelmReleases whose Ready condition is True.")
            .grid_pos(dashboard.GridPos(h=5, w=6, x=12, y=0))
            .with_target(
                mimir_query(
                    'count(kube_customresource_flux_helmrelease_ready{namespace=~"$namespace",status="True"})', "ready"
                ).instant()
            )
        )
        .with_panel(
            _readiness_stat()
            .id(4)
            .title("Ready GitRepositories")
            .description("GitRepositories whose Ready condition is True.")
            .grid_pos(dashboard.GridPos(h=5, w=6, x=18, y=0))
            .with_target(
                mimir_query(
                    'count(kube_customresource_flux_gitrepository_ready{namespace=~"$namespace",status="True"})',
                    "ready",
                ).instant()
            )
        )
        .with_panel(
            _history_panel()
            .id(5)
            .title("Reconciliation duration by resource kind")
            .grid_pos(dashboard.GridPos(h=9, w=12, x=0, y=5))
            .unit("s")
            .line_interpolation(common.LineInterpolation.SMOOTH)
            .with_target(
                mimir_query(
                    "sum(rate(gotk_reconcile_duration_seconds_sum[5m])) by (kind) / "
                    "sum(rate(gotk_reconcile_duration_seconds_count[5m])) by (kind)",
                    "{{kind}}",
                )
            )
        )
        .with_panel(
            _history_panel()
            .id(6)
            .title("Reconciliations per second")
            .grid_pos(dashboard.GridPos(h=9, w=12, x=12, y=5))
            .unit("ops")
            .with_target(mimir_query("sum(rate(gotk_reconcile_duration_seconds_count[5m])) by (kind)", "{{kind}}"))
        )
        .with_panel(
            _history_panel()
            .id(7)
            .title("Not-ready resources by namespace")
            .grid_pos(dashboard.GridPos(h=8, w=12, x=0, y=14))
            .unit("short")
            .with_target(
                mimir_query(
                    'sum by (namespace) (kube_customresource_flux_kustomization_ready{namespace=~"$namespace",status!="True"})',
                    "Kustomization {{namespace}}",
                )
            )
            .with_target(
                mimir_query(
                    'sum by (namespace) (kube_customresource_flux_helmrelease_ready{namespace=~"$namespace",status!="True"})',
                    "HelmRelease {{namespace}}",
                    "B",
                )
            )
            .with_target(
                mimir_query(
                    'sum by (namespace) (kube_customresource_flux_gitrepository_ready{namespace=~"$namespace",status!="True"})',
                    "GitRepository {{namespace}}",
                    "C",
                )
            )
        )
        .with_panel(
            _history_panel()
            .id(8)
            .title("GitRepository artifact size")
            .grid_pos(dashboard.GridPos(h=8, w=12, x=12, y=14))
            .unit("decbytes")
            .tooltip(VizTooltipOptions())
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


def main() -> None:
    print(JSONEncoder(indent=2).encode(build_dashboard()))


if __name__ == "__main__":
    main()
