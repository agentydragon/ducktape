"""GitHub API quotas dashboard."""

from grafana_foundation_sdk.models import common, dashboard, stat

from cluster.cdk8s.monitoring.dashboards.foundation import last_value, mimir, mimir_query, series_options


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="github-api-quotas",
        title="GitHub API quotas",
        tags=["github", "quotas"],
        editable=False,
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-7d"),
        refresh="5m",
        schema_version=41,
        panels=[
            dashboard.Panel(
                type_val="stat",
                id_val=1,
                targets=[
                    mimir_query(
                        "100 * github_graphql_rate_remaining / on (github_account) github_graphql_rate_limit",
                        legend="{{github_account}}",
                    ).build()
                ],
                title="GraphQL quota remaining",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=6, w=24),
                options=stat.Options(reduce_options=last_value(), orientation=common.VizOrientation.HORIZONTAL),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="percent",
                        min_val=0,
                        max_val=100,
                        thresholds=dashboard.ThresholdsConfig(
                            steps=[
                                dashboard.Threshold(color="red"),
                                dashboard.Threshold(value=20, color="yellow"),
                                dashboard.Threshold(value=50, color="green"),
                            ]
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=2,
                targets=[
                    mimir_query("github_graphql_rate_remaining", legend="{{github_account}} remaining").build(),
                    mimir_query("github_graphql_rate_limit", legend="{{github_account}} limit", ref_id="B").build(),
                ],
                title="GraphQL quota history",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=6),
                options=series_options(legend_calcs=("lastNotNull", "min")),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, fill_opacity=15
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=3,
                targets=[
                    mimir_query(
                        'github_rate_remaining{resource!="graphql"}', legend="{{github_account}} / {{resource}}"
                    ).build()
                ],
                title="Remaining quota by REST API resource",
                description="From REST /rate_limit. `graphql` is excluded: that endpoint reports the GraphQL bucket as permanently full even while GraphQL is exhausted. The GraphQL panels above read GitHub's GraphQL response headers instead.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=16),
                options=series_options(legend_calcs=("lastNotNull",)),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=1, fill_opacity=10
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    mimir_query("github_graphql_rate_used", legend="{{github_account}} used").build(),
                    mimir_query("github_graphql_rate_limit", legend="{{github_account}} limit", ref_id="B").build(),
                ],
                title="GraphQL points used this window",
                description="GraphQL points consumed in the current window. This can exceed the limit: GitHub keeps counting requests it is already rejecting.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=26),
                options=series_options(legend_calcs=("lastNotNull", "max")),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, fill_opacity=10
                        ),
                    )
                ),
            ),
        ],
    )
