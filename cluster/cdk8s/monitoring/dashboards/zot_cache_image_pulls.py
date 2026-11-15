"""Zot cache and image pulls dashboard."""

from grafana_foundation_sdk.models import bargauge, common, dashboard, loki, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    HeaderLink,
    last_value,
    loki_datasource,
    mimir,
    mimir_query,
    series_options,
    stat_options,
)


def _stat_fields(unit: str) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit=unit, min_val=0, color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC)
        )
    )


def _series_fields(unit: str) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit=unit,
            min_val=0,
            custom=common.GraphFieldConfig(
                draw_style=common.GraphDrawStyle.LINE,
                line_width=1,
                line_interpolation=common.LineInterpolation.LINEAR,
                fill_opacity=10,
                show_points=common.VisibilityMode.NEVER,
            ),
        )
    )


def _bar_options() -> bargauge.Options:
    return bargauge.Options(
        display_mode=common.BarGaugeDisplayMode.BASIC,
        reduce_options=last_value(),
        orientation=common.VizOrientation.HORIZONTAL,
    )


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="zot-cache-image-pulls",
        title="Zot cache and image pulls",
        description="Zot pull-through cache usage, outer-cluster image pull activity, cache storage and garbage collection. Manifest reads and runtime pull calls have different scopes; no cache-hit ratio is implied.",
        tags=["zot", "oci-cache", "images", "storage"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="1m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=10,
                title="Reading this comparison",
                grid_pos=dashboard.GridPos(h=3, w=24),
                options=text.Options(
                    content="Zot **client requests**, internal **manifest reads**, and outer-cluster **runtime pull calls** have different units and scopes. Nested K3s and Docker-in-Docker are absent from the kubelet total. **Blob bytes served include cache misses**; these counters do not establish a cache-hit ratio or bytes saved. Totals use the **24 hours ending at the selected time**; graphs follow the selected range."
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=1,
                targets=[
                    loki.Dataquery(
                        expr='sum(count_over_time({namespace="oci-cache",container="zot"} |= "HTTP API" | json | message="HTTP API" | method="GET" | path=~"/v2/.*/manifests/.*" | statusCode="200" | __error__="" [24h]))',
                        legend_format="Client manifest GETs",
                        ref_id="A",
                        query_type="instant",
                        datasource=loki_datasource(),
                    )
                ],
                title="Client manifest GETs · 24h",
                description="Successful HTTP GET /v2/.../manifests/... requests in Zot access logs. Excludes HEAD and internal image-store reads. A multi-architecture image can require multiple manifests; this is not a unique or complete image-pull count. Fixed 24h window ending at the selected time.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=4, w=6, y=3),
                options=stat_options(),
                field_config=_stat_fields("short"),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query(
                        'sum(increase(kubelet_runtime_operations_total{job="kubelet",operation_type="pull_image"}[24h]))',
                        legend="Runtime pull calls",
                    )
                    .instant()
                    .build()
                ],
                title="Kubelet pull calls · 24h",
                description="Outer-cluster kubelet CRI pull_image calls across all registries, including failures. Does not cover nested K3s or Docker-in-Docker. No registry label is available, so this is not a denominator for Zot cache coverage. Window ends at the selected time.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=6, y=3),
                options=stat_options(),
                field_config=_stat_fields("short"),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[
                    mimir_query(
                        'max(kubelet_volume_stats_used_bytes{namespace="oci-cache",persistentvolumeclaim="zot-cache"})',
                        legend="Used",
                    )
                    .instant()
                    .build()
                ],
                title="Cache filesystem used",
                description="Kubelet filesystem usage of zot-cache, including indexes, staging and metadata. One filesystem copy; Piraeus replicas are not added.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=12, y=3),
                options=stat_options(),
                field_config=_stat_fields("bytes"),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=4,
                targets=[
                    loki.Dataquery(
                        expr='sum(sum_over_time({namespace="oci-cache",container="zot"} |= "HTTP API" | json | message="HTTP API" | method="GET" | path=~"/v2/.*/blobs/.*" | statusCode=~"200|206" | unwrap bodySize | __error__="" [24h]))',
                        legend_format="Blob response bytes",
                        ref_id="A",
                        query_type="instant",
                        datasource=loki_datasource(),
                    )
                ],
                title="Blob bytes served · 24h",
                description="Sum of bodySize for successful HTTP GET blob responses (200 or 206) in Zot access logs. Includes repeated and partial transfers. This is client-facing response payload, not upstream bytes avoided; cold-cache responses count too. Excludes HTTP/TLS overhead. Fixed 24h window ending at the selected time.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=4, w=6, x=18, y=3),
                options=stat_options(),
                field_config=_stat_fields("bytes"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    mimir_query(
                        'sum(increase(zot_repo_downloads_total{namespace="oci-cache"}[1h]))',
                        legend="Zot manifest reads",
                    ).build(),
                    mimir_query(
                        'sum(increase(kubelet_runtime_operations_total{job="kubelet",operation_type="pull_image"}[1h]))',
                        legend="Outer-cluster runtime pull calls",
                        ref_id="B",
                    ).build(),
                ],
                title="Manifest reads and runtime pull calls · rolling 1h",
                description="Zot counts image-store manifest reads, including HEAD checks, multi-architecture manifests and internal reads. These are not complete image pulls, cache hits or unique images. Outer-cluster kubelet CRI pull_image calls across all registries, including failures. Does not cover nested K3s or Docker-in-Docker. No registry label is available, so this is not a denominator for Zot cache coverage. Each point is the preceding hour, not a disjoint bucket.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=7),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields("short"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=6,
                targets=[
                    mimir_query(
                        'max(kubelet_volume_stats_used_bytes{namespace="oci-cache",persistentvolumeclaim="zot-cache"})',
                        legend="Used",
                    ).build(),
                    mimir_query(
                        'max(kubelet_volume_stats_capacity_bytes{namespace="oci-cache",persistentvolumeclaim="zot-cache"})',
                        legend="Capacity",
                        ref_id="B",
                    ).build(),
                ],
                title="Cache filesystem used and capacity",
                description="Filesystem metrics for the Zot PVC, measured by kubelet. Filesystem capacity can be slightly below the PVC request. Physical replicated allocation is separate.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=7),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields("bytes"),
            ),
            dashboard.Panel(
                type_val="bargauge",
                id_val=7,
                targets=[
                    mimir_query(
                        'sort_desc(sum by (repo) (increase(zot_repo_downloads_total{namespace="oci-cache"}[24h])))',
                        legend="{{repo}}",
                    )
                    .instant()
                    .build()
                ],
                title="Manifest reads by repository · 24h",
                description="Zot counts image-store manifest reads, including HEAD checks, multi-architecture manifests and internal reads. These are not complete image pulls, cache hits or unique images. Fixed 24h window ending at the selected time. Repositories with zero reads remain visible when the counter exists.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, y=15),
                options=_bar_options(),
                field_config=_stat_fields("short"),
            ),
            dashboard.Panel(
                type_val="bargauge",
                id_val=8,
                targets=[
                    mimir_query(
                        'sort_desc(sum by (repo) (zot_repo_storage_bytes{namespace="oci-cache"}))', legend="{{repo}}"
                    )
                    .instant()
                    .build()
                ],
                title="Repository logical bytes",
                description="Zot walks each repository directory and sums file sizes. Hard-linked shared blobs can be counted more than once, and metadata outside repository directories is excluded. Do not add this to filesystem usage or interpret it as physical replicated allocation.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, x=12, y=15),
                options=_bar_options(),
                field_config=_stat_fields("bytes"),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=9,
                targets=[
                    mimir_query(
                        'sum by (type) (increase(zot_gc_deleted_total{namespace="oci-cache"}[1h]))', legend="{{type}}"
                    ).build()
                ],
                title="GC deletions · rolling 1h",
                description="Items deleted by Zot GC, split into blobs and manifests. Counts are not bytes reclaimed. Each point is the preceding hour, so a daily sweep appears briefly and zero between sweeps is expected.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=8, y=25),
                options=series_options(sort=common.SortOrder.DESCENDING, legend_calcs=("lastNotNull",)),
                field_config=_series_fields("short"),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=11,
                targets=[
                    loki.Dataquery(
                        expr='sum by (client) (count_over_time({namespace="oci-cache",container="zot"} |= "HTTP API" | json message="message", method="method", path="path", status="statusCode", agent="headers[\\"User-Agent\\"][0]" | message="HTTP API" | method="GET" | path=~"/v2/.*/blobs/.*" | status=~"200|206" | line_format "{{.agent}}" | regexp `(?P<client>[^/ ]+)/` | __error__="" [24h]))',
                        legend_format="{{client}}",
                        ref_id="A",
                        query_type="instant",
                        datasource=loki_datasource(),
                    )
                ],
                title="Blob GETs by client · 24h",
                description="Successful blob HTTP GET responses (200 or 206), grouped by the first User-Agent product. Confirms real client traffic and includes retries or partial transfers. Clients identify themselves; this does not identify the requesting workload or count unique image pulls. Fixed 24h window ending at the selected time.",
                datasource=loki_datasource(),
                grid_pos=dashboard.GridPos(h=8, x=12, y=25),
                transformations=[
                    dashboard.DataTransformerConfig(id_val="labelsToFields", options={}),
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "renameByName": {"client": "Client", "Value": "Blob GETs", "Value #A": "Blob GETs"},
                        },
                    ),
                ],
                field_config=_stat_fields("short"),
            ),
        ],
        links=[
            HeaderLink(
                title="Cache configuration and retention",
                url="https://github.com/agentydragon/ducktape/blob/devel/cluster/k8s/oci-cache/README.md",
                target_blank=True,
            ),
            HeaderLink(
                title="Zot manifest counter implementation",
                url="https://github.com/project-zot/zot/blob/v2.1.21/pkg/storage/imagestore/imagestore.go#L519",
                target_blank=True,
            ),
        ],
    )
