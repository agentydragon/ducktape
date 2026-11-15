"""Host filesystem usage · ext4/XFS dashboard."""

from grafana_foundation_sdk.models import barchart, bargauge, common, dashboard, piechart, stat, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    field_override,
    hide_time,
    last_value,
    mimir,
    mimir_query,
    mimir_table_query,
    series_options,
    storage_tier_variable,
)

_COMPONENT_BYTES = '(sum by (node, device, component) (disk_usage:component_used_bytes{node=~"$node"}) or label_replace(disk_usage:unattributed_bytes{node=~"$node"}, "component", "Unattributed", "", "") or label_replace(disk_usage:filesystem_free_bytes{node=~"$node"} - disk_usage:filesystem_avail_bytes{node=~"$node"}, "component", "Reserved (root only)", "", "") or label_replace(disk_usage:filesystem_avail_bytes{node=~"$node"}, "component", "Free", "", ""))'
_MOUNT_MEDIA = '(topk by (node, device) (1, disk_usage:mount:info{node=~"$node"}) * on (node, device) group_left (media) disk_usage:filesystem:info{node=~"$node"})'
_FILESYSTEM_SIZE = 'disk_usage:filesystem_size_bytes{node=~"$node"}'
_FILESYSTEM_FREE = 'disk_usage:filesystem_free_bytes{node=~"$node"}'
_FILESYSTEM_AVAILABLE = 'disk_usage:filesystem_avail_bytes{node=~"$node"}'


def _filesystem_bytes_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="bytes", color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="#5794F2")
        )
    )


def _component_color_overrides() -> list[dashboard.DashboardFieldConfigSourceOverrides]:
    return [
        field_override("Free", {"color": {"mode": "fixed", "fixedColor": "rgba(115, 191, 105, 0.35)"}}),
        field_override("Unattributed", {"color": {"mode": "fixed", "fixedColor": "orange"}}),
        field_override("Reserved (root only)", {"color": {"mode": "fixed", "fixedColor": "#8e8e8e"}}),
    ]


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="cluster-disk-usage",
        title="Host filesystem usage · ext4/XFS",
        tags=["cluster", "disk", "storage", "filesystem", "ext4", "xfs"],
        graph_tooltip=dashboard.DashboardCursorSync.CROSSHAIR,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="5m",
        schema_version=39,
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read this dashboard",
                grid_pos=dashboard.GridPos(h=6, w=24),
                options=text.Options(
                    content="This dashboard covers **host ext4/XFS filesystems** only. Its filesystem totals and attribution do not include Piraeus LVM thin pools, diskful PV limits, or copy estimates; see the **Piraeus LVM storage** dashboard for those separate accounting layers.\n\nFilesystem use is size minus free bytes, deduplicated by node and device. The attribution view separates measured components, unattributed bytes, root-reserved free space, and available bytes. Repeated mounts can refer to one underlying device.\n\nThe Storage tier selector filters by node labels; Machine filters node-owned panels. This is node-level filtering. If a node later has both SSD and HDD, per-device media labels will be needed to separate those disks."
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    mimir_query(f"sum({_FILESYSTEM_SIZE} - on (node, device) {_FILESYSTEM_FREE})").instant().build()
                ],
                title="Host filesystem used (ext4/XFS)",
                description="Sum of ext4/XFS filesystem size minus free bytes, deduplicated by node and device. This excludes ext4 root-reserved blocks and is host-filesystem accounting, separate from Piraeus thin-pool use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, y=6),
                options=stat.Options(reduce_options=last_value()),
                field_config=_filesystem_bytes_fields(),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[mimir_query(f"sum({_FILESYSTEM_SIZE})").instant().build()],
                title="Host filesystem capacity (ext4/XFS)",
                description="Total ext4/XFS host filesystem capacity from filesystem recording rules, deduplicated by node and device.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=5, x=12, y=6),
                options=stat.Options(reduce_options=last_value()),
                field_config=_filesystem_bytes_fields(),
            ),
            dashboard.RowPanel(
                title="Ext4/XFS host filesystem attribution", grid_pos=dashboard.GridPos(h=1, w=24, y=11), id_val=23
            ),
            dashboard.Panel(
                type_val="piechart",
                id_val=18,
                targets=[
                    mimir_query(
                        'sum by (category) (disk_usage:component_used_bytes{node=~"$node"})', legend="{{category}}"
                    )
                    .instant()
                    .build(),
                    mimir_query('sum(disk_usage:unattributed_bytes{node=~"$node"})', legend="Unattributed", ref_id="B")
                    .instant()
                    .build(),
                    mimir_query(
                        f"sum({_FILESYSTEM_FREE} - on (node, device) {_FILESYSTEM_AVAILABLE})",
                        legend="Reserved (root only)",
                        ref_id="C",
                    )
                    .instant()
                    .build(),
                    mimir_query(f"sum({_FILESYSTEM_AVAILABLE})", legend="Free", ref_id="D").instant().build(),
                ],
                title="Ext4/XFS filesystem capacity by use",
                description="Host filesystem components, unattributed use, root-only reserve, and available bytes. Piraeus thin-pool physical use and volume-copy estimates are separate accounting layers.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=12),
                options=piechart.Options(
                    display_labels=[piechart.PieChartLabels.NAME, piechart.PieChartLabels.PERCENT],
                    reduce_options=last_value(),
                    legend=piechart.PieChartLegendOptions(
                        show_legend=True,
                        values=[piechart.PieChartLegendValues.VALUE, piechart.PieChartLegendValues.PERCENT],
                        display_mode=common.LegendDisplayMode.TABLE,
                        placement=common.LegendPlacement.RIGHT,
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes"), overrides=_component_color_overrides()
                ),
            ),
            dashboard.Panel(
                type_val="barchart",
                id_val=19,
                targets=[
                    mimir_table_query(
                        f'label_join({_COMPONENT_BYTES} * on (node, device) group_left (mountpoint, media) {_MOUNT_MEDIA}, "disk", " · ", "node", "device", "mountpoint", "media")'
                    ).build(),
                    mimir_table_query(f"max({_FILESYSTEM_SIZE})", ref_id="MaxDisk").build(),
                ],
                title="Host filesystem use by component",
                description="One bar per scraped ext4/XFS filesystem across the selected machines, labelled node, device, mountpoint, and media. Bars share the largest selected disk as their scale. Components come from existing filesystem rules; Piraeus pool physical use and per-copy estimates are separate.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=12),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="configFromData",
                        options={
                            "configRefId": "MaxDisk",
                            "mappings": [
                                {"fieldName": "Value #MaxDisk", "handlerKey": "max"},
                                {"fieldName": "Time", "handlerKey": "__ignore"},
                            ],
                            "applyTo": {"id": "byType", "options": "number"},
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="groupingToMatrix",
                        options={
                            "columnField": "component",
                            "rowField": "disk",
                            "valueField": "Value #A",
                            "emptyValue": "null",
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="sortBy", options={"fields": {}, "sort": [{"field": "disk\\component", "desc": False}]}
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize", options={"excludeByName": {}, "renameByName": {"disk\\component": "Disk"}}
                    ),
                ],
                options=barchart.Options(
                    x_field="Disk",
                    orientation=common.VizOrientation.HORIZONTAL,
                    stacking=common.StackingMode.NORMAL,
                    show_value=common.VisibilityMode.NEVER,
                    bar_width=0.8,
                    tooltip=common.VizTooltipOptions(
                        mode=common.TooltipDisplayMode.MULTI, sort=common.SortOrder.DESCENDING, hide_zeros=True
                    ),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes",
                        min_val=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC),
                        custom=barchart.FieldConfig(
                            line_width=0, fill_opacity=85, axis_placement=common.AxisPlacement.AUTO, axis_soft_min=0
                        ),
                    ),
                    overrides=_component_color_overrides(),
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=20,
                targets=[
                    mimir_table_query(
                        f"sort_desc({_COMPONENT_BYTES} * on (node, device) group_left (mountpoint, media) {_MOUNT_MEDIA})"
                    ).build(),
                    mimir_table_query(
                        f"{_COMPONENT_BYTES} * on (node, device) group_left (mountpoint, media) {_MOUNT_MEDIA} / on (node, device) group_left () {_FILESYSTEM_SIZE}",
                        ref_id="B",
                    ).build(),
                ],
                title="Host filesystem attribution by node and device",
                description="Ext4/XFS filesystem component estimates, free bytes, and root-only reserve by node and device. Piraeus thin-pool accounting is kept separate; per-copy Piraeus allocation estimates do not form a residual against these filesystem bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=12, w=24, y=21),
                transformations=[
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "indexByName": {
                                name: index
                                for index, name in enumerate(
                                    ("node", "device", "mountpoint", "media", "component", "Value #A", "Value #B")
                                )
                            },
                            "renameByName": {
                                "node": "Machine",
                                "device": "Device",
                                "mountpoint": "Mountpoint",
                                "media": "Media",
                                "component": "Component",
                                "Value #A": "Bytes",
                                "Value #B": "Share of disk",
                            },
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="sortBy", options={"fields": {}, "sort": [{"field": "Bytes", "desc": True}]}
                    ),
                ],
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes"),
                    overrides=[field_override("Share of disk", {"unit": "percentunit", "decimals": 1})],
                ),
            ),
            dashboard.Panel(
                type_val="bargauge",
                id_val=21,
                targets=[
                    mimir_query(
                        f'sort_desc(label_join(disk_usage:unattributed_bytes{{node=~"$node"}} * on (node, device) group_left (mountpoint, media) {_MOUNT_MEDIA}, "disk", " · ", "node", "device", "mountpoint", "media"))',
                        legend="{{disk}}",
                    )
                    .instant()
                    .build()
                ],
                title="Unattributed bytes per disk",
                description="Filesystem used (size minus free) minus every attributed component. On system disks (/var, /) this is mostly container images, emptyDir volumes, logs and etcd, which no scraped metric breaks down; on data disks, see the PVC list beside it.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=33),
                options=bargauge.Options(
                    display_mode=common.BarGaugeDisplayMode.BASIC,
                    name_placement=common.BarGaugeNamePlacement.TOP,
                    reduce_options=last_value(),
                    orientation=common.VizOrientation.HORIZONTAL,
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="bytes",
                        min_val=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="orange"),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=4,
                targets=[
                    mimir_table_query(
                        f'sort_desc(({_FILESYSTEM_SIZE} - on (node, device) {_FILESYSTEM_FREE}) * on (node, device) group_right disk_usage:mount:info{{node=~"$node"}})',
                        legend="{{node}} {{device}} {{mountpoint}}",
                    ).build()
                ],
                title="Current host filesystem use by mount",
                description="Per-mount ext4/XFS data used from deduplicated per-device filesystem recordings. Repeated mounts, including bind mounts, are shown as detail rows; dashboard totals count each device once.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=44),
                transformations=[hide_time()],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    mimir_query(
                        f'topk(10, ({_FILESYSTEM_SIZE} - on (node, device) {_FILESYSTEM_FREE}) * on (node, device) group_right disk_usage:mount:info{{node=~"$node"}})',
                        legend="{{node}} {{mountpoint}}",
                    ).build()
                ],
                title="Largest host filesystem mounts over time",
                description="Top ten ext4/XFS mounts by data used, joined from deduplicated per-device filesystem recordings. Repeated mounts can show the same underlying device; they are detail series, not additive capacity.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=44),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                storage_tier_variable(),
                dashboard.VariableModel(
                    name="node",
                    label="Machine",
                    query='label_values(kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"}, node)',
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition='label_values(kube_node_labels{label_storage_allegedly_works_tier=~"$storage_tier"}, node)',
                ),
            ]
        ),
    )
