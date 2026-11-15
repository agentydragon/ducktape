"""Piraeus LVM storage dashboard."""

from grafana_foundation_sdk.models import barchart, common, dashboard, stat, text

from cluster.cdk8s.monitoring.dashboards.foundation import (
    field_override,
    last_value,
    mimir,
    mimir_query,
    mimir_table_query,
    series_options,
)

_POOL_COLORS = (("ssd", "#5794F2"), ("hdd", "#FF9830"))
_POOL_SELECTION = 'node=~"$node", storage_pool=~"$storage_pool"'
_BOUND_PVC_LIMIT = 'max by (namespace, persistentvolumeclaim) (disk_usage:volume_capacity_bytes{phase="Bound", storage_pool=~"$storage_pool"})'


def _pool_series_fields() -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="bytes", color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC)
        ),
        overrides=[
            *_pool_color_overrides(regex=True),
            field_override("/ capacity$/", {"custom.lineStyle": {"dash": [10, 10], "fill": "dash"}}, regex=True),
        ],
    )


def _pool_stat_transforms() -> list[dashboard.DataTransformerConfig]:
    return [
        dashboard.DataTransformerConfig(id_val="sortBy", options={"sort": [{"field": "storage_pool", "desc": False}]}),
        dashboard.DataTransformerConfig(id_val="filterFieldsByName", options={"exclude": {"names": ["Time"]}}),
        dashboard.DataTransformerConfig(id_val="rowsToFields", options={}),
    ]


def _pool_stat(id: int, title: str, expr: str, description: str, unit: str) -> dashboard.Panel:
    index = id - 26
    return dashboard.Panel(
        type_val="stat",
        id_val=id,
        title=title,
        description=description,
        datasource=mimir(),
        targets=[mimir_table_query(expr, legend="{{storage_pool}}").build()],
        grid_pos=dashboard.GridPos(h=4, w=8, x=(index % 3) * 8, y=7 + (index // 3) * 4),
        transformations=_pool_stat_transforms(),
        options=stat.Options(reduce_options=last_value()),
        field_config=_pool_fields(unit),
    )


def _pool_fields(unit: str = "bytes") -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit=unit, color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.PALETTE_CLASSIC)
        ),
        overrides=_pool_color_overrides(),
    )


def _pool_color_overrides(*, regex: bool = False) -> list[dashboard.DashboardFieldConfigSourceOverrides]:
    return [
        field_override(f"/^{pool} /" if regex else pool, {"color": {"mode": "fixed", "fixedColor": color}}, regex=regex)
        for pool, color in _POOL_COLORS
    ]


def _pool_metric(metric: str, *, node: str = "$node") -> str:
    return f'disk_usage:pool_{metric}_bytes{{node=~"{node}", storage_pool=~"$storage_pool"}}'


def _pool_total(metric: str) -> str:
    return f"sum by (storage_pool) ({_pool_metric(metric)})"


def _table_series(ref_ids: str, labels: tuple[str, ...]) -> list[dashboard.DataTransformerConfig]:
    return [
        dashboard.DataTransformerConfig(
            id_val=transform,
            filter_val=dashboard.MatcherConfig(id_val="byRefId", options=f"/^{ref_id}$/"),
            topic="series",
            options=options,
        )
        for ref_id in ref_ids
        for transform, options in (
            ("labelsToFields", {}),
            ("filterFieldsByName", {"include": {"names": ["Time", *labels, f"Value #{ref_id}"]}}),
        )
    ]


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="piraeus-storage",
        title="Piraeus LVM storage",
        tags=["cluster", "storage", "piraeus", "lvm"],
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
                    content="This dashboard covers **Piraeus LVM thin-pool and volume accounting**. Thin-pool used, capacity, and free bytes are physical pool measurements. Diskful PV limits are a separate commitment measure; their ratio to raw capacity is not physical-use percentage and can exceed 100%. Per-copy allocation estimates do not decompose physical pool use.\n\nThe PVC table shows kubelet-reported filesystem use on the consumer node alongside a Bound PV capacity limit. It is separate from both host ext4/XFS filesystem accounting and thin-pool physical use. Raw-block or unmounted PVCs may have no filesystem-use sample.\n\nStorage pool selects LINSTOR pool names across storage nodes, including machines with multiple pools. Machine filters storage nodes for pool and copy panels and consumer nodes for the PVC table. PVCs with copies in multiple pools appear under each selected pool. The ssd pool is blue; hdd is orange."
                ),
            ),
            dashboard.RowPanel(title="Piraeus thin pools", grid_pos=dashboard.GridPos(h=1, w=24, y=6), id_val=25),
            *[
                _pool_stat(index + 26, title, expr, description, unit)
                for index, (title, expr, description, unit) in enumerate(
                    [
                        (
                            "Physical pool used",
                            _pool_total("used"),
                            "Piraeus thin-pool physical use, summed over the selected storage nodes by pool name.",
                            "bytes",
                        ),
                        (
                            "Physical pool capacity",
                            _pool_total("capacity"),
                            "Raw Piraeus thin-pool capacity. This is separate from ext4/XFS filesystem capacity.",
                            "bytes",
                        ),
                        (
                            "Physical pool free",
                            _pool_total("free"),
                            "Piraeus thin-pool physical free bytes from the pool accounting rules.",
                            "bytes",
                        ),
                        (
                            "Diskful copy limits",
                            _pool_total("committed"),
                            "Sum of per-copy PV capacity limits committed on the selected pools; this is not physical use.",
                            "bytes",
                        ),
                        (
                            "Uncommitted limit headroom",
                            f"{_pool_total('capacity')} - {_pool_total('committed')}",
                            "Raw pool capacity minus diskful copy capacity limits. This limit comparison may be negative; it is not physical free space.",
                            "bytes",
                        ),
                        (
                            "Copy limits / pool capacity",
                            f"{_pool_total('committed')} / {_pool_total('capacity')}",
                            "Diskful PV capacity limits divided by raw pool capacity. This is not a physical-use percentage and can exceed 100%.",
                            "percentunit",
                        ),
                    ]
                )
            ],
            dashboard.Panel(
                type_val="table",
                id_val=34,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        ("A", _pool_metric("capacity")),
                        ("B", _pool_metric("used")),
                        ("C", f"{_pool_metric('used')} / on (node, storage_pool) {_pool_metric('capacity')}"),
                        ("D", _pool_metric("committed")),
                        ("E", f"{_pool_metric('committed')} / on (node, storage_pool) {_pool_metric('capacity')}"),
                        ("F", f"{_pool_metric('capacity')} - on (node, storage_pool) {_pool_metric('committed')}"),
                    )
                ],
                title="Piraeus thin-pool accounting by storage node",
                description="Physical pool usage and capacity are shown separately from the sum of diskful PV limits. Uncommitted headroom is capacity minus those limits and may be negative.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=10, w=24, y=15),
                transformations=[
                    *_table_series("ABCDEF", ("node", "storage_pool")),
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "indexByName": {
                                "node": 0,
                                "storage_pool": 1,
                                "Value #A": 3,
                                "Value #B": 4,
                                "Value #C": 2,
                                "Value #D": 5,
                                "Value #E": 6,
                                "Value #F": 7,
                            },
                            "renameByName": {
                                "node": "Storage node",
                                "storage_pool": "Pool",
                                "Value #A": "Physical capacity",
                                "Value #B": "Physical used",
                                "Value #C": "Physical used %",
                                "Value #D": "Diskful copy limits",
                                "Value #E": "Copy limits / capacity",
                                "Value #F": "Uncommitted limit headroom",
                            },
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="sortBy", options={"fields": {}, "sort": [{"field": "Physical used", "desc": True}]}
                    ),
                ],
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes"),
                    overrides=[
                        field_override("Copy limits / capacity", {"unit": "percentunit", "decimals": 1}),
                        field_override("Physical used %", {"unit": "percentunit", "decimals": 1}),
                    ],
                ),
            ),
            dashboard.RowPanel(
                title="Thin pools on $pool_node",
                grid_pos=dashboard.GridPos(h=1, w=24, y=25),
                id_val=40,
                repeat="pool_node",
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=32,
                targets=[
                    mimir_query(_pool_metric("used", node="$pool_node"), legend="{{storage_pool}} used").build(),
                    mimir_query(
                        _pool_metric("capacity", node="$pool_node"), legend="{{storage_pool}} capacity", ref_id="B"
                    ).build(),
                ],
                title="Physical pool usage",
                description="Physical bytes used and raw capacity per storage node and pool. These series do not include per-volume estimates or ext4/XFS host filesystem bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_pool_series_fields(),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=33,
                targets=[
                    mimir_query(
                        _pool_metric("committed", node="$pool_node"), legend="{{storage_pool}} copy limits"
                    ).build(),
                    mimir_query(
                        _pool_metric("capacity", node="$pool_node"), legend="{{storage_pool}} capacity", ref_id="B"
                    ).build(),
                ],
                title="Diskful copy limits",
                description="PV capacity limits of diskful copies compared with raw pool capacity, per storage node and pool. These limits are not allocated bytes.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(x=12, y=26),
                options=series_options(sort=common.SortOrder.DESCENDING),
                field_config=_pool_series_fields(),
            ),
            dashboard.RowPanel(
                title="Piraeus diskful volume copies", grid_pos=dashboard.GridPos(h=1, w=24, y=35), id_val=35
            ),
            dashboard.Panel(
                type_val="barchart",
                id_val=36,
                targets=[
                    mimir_table_query(
                        f'topk(15, (label_join(label_join(label_join(sum by (node, storage_pool, phase, resource, persistentvolume, namespace, persistentvolumeclaim) (disk_usage:volume_allocated_bytes{{{_POOL_SELECTION}, persistentvolumeclaim!=""}}), "copy", "/", "namespace", "persistentvolumeclaim"), "location", "/", "node", "storage_pool"), "copy", " · ", "copy", "location", "phase")) or (label_join(label_join(label_join(sum by (node, storage_pool, phase, resource, persistentvolume, namespace, persistentvolumeclaim) (disk_usage:volume_allocated_bytes{{{_POOL_SELECTION}, persistentvolumeclaim=""}}), "copy", "/", "namespace", "resource"), "location", "/", "node", "storage_pool"), "copy", " · ", "copy", "location", "phase")))',
                        legend="{{copy}}",
                    ).build()
                ],
                title="15 largest estimated diskful copy allocations",
                description="Top 15 per-copy allocation estimates on selected Piraeus storage nodes. Labels show namespace/PVC, node/pool, and PV phase; copies without a PVC use the LINSTOR resource name. These estimates are not a decomposition of pool physical use.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=12, w=24, y=36),
                transformations=[
                    dashboard.DataTransformerConfig(
                        id_val="groupingToMatrix",
                        options={
                            "columnField": "storage_pool",
                            "rowField": "copy",
                            "valueField": "Value",
                            "emptyValue": "null",
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {},
                            "indexByName": {},
                            "renameByName": {"copy\\storage_pool": "copy"},
                        },
                    ),
                ],
                options=barchart.Options(
                    x_field="copy",
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
                    overrides=_pool_color_overrides(),
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=37,
                targets=[
                    mimir_table_query(
                        f"sum by (node, storage_pool, phase, resource, persistentvolume, namespace, persistentvolumeclaim) (disk_usage:volume_allocated_bytes{{{_POOL_SELECTION}}})"
                    ).build(),
                    mimir_table_query(
                        f"max by (node, storage_pool, phase, resource, persistentvolume, namespace, persistentvolumeclaim) (disk_usage:volume_capacity_bytes{{{_POOL_SELECTION}}})",
                        ref_id="B",
                    ).build(),
                ],
                title="Diskful copy allocation estimates and PV limits",
                description="One row per storage-node copy, including retained or unmounted resources without a current PVC identity. Estimated allocation and PV capacity limit are separate values; PV phase distinguishes Bound, Released, and Available copies.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=14, w=24, y=48),
                transformations=[
                    *_table_series(
                        "AB",
                        (
                            "node",
                            "storage_pool",
                            "phase",
                            "resource",
                            "persistentvolume",
                            "namespace",
                            "persistentvolumeclaim",
                        ),
                    ),
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "indexByName": {
                                name: index
                                for index, name in enumerate(
                                    (
                                        "node",
                                        "storage_pool",
                                        "namespace",
                                        "persistentvolumeclaim",
                                        "Value #A",
                                        "Value #B",
                                        "phase",
                                        "resource",
                                        "persistentvolume",
                                    )
                                )
                            },
                            "renameByName": {
                                "node": "Storage node",
                                "storage_pool": "Pool",
                                "namespace": "Namespace",
                                "persistentvolumeclaim": "PVC",
                                "Value #A": "Estimated allocated bytes",
                                "Value #B": "PV capacity limit",
                                "phase": "PV phase",
                                "resource": "LINSTOR resource",
                                "persistentvolume": "PV",
                            },
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="sortBy",
                        options={"fields": {}, "sort": [{"field": "Estimated allocated bytes", "desc": True}]},
                    ),
                ],
                field_config=dashboard.FieldConfigSource(defaults=dashboard.FieldConfig(unit="bytes")),
            ),
            dashboard.RowPanel(
                title="Piraeus consumer-side PVC filesystem usage",
                grid_pos=dashboard.GridPos(h=1, w=24, y=62),
                id_val=38,
            ),
            dashboard.Panel(
                type_val="table",
                id_val=39,
                targets=[
                    mimir_table_query(expr, ref_id=ref_id).build()
                    for ref_id, expr in (
                        (
                            "A",
                            f'disk_usage:pvc_used_bytes{{node=~"$node"}} and on (namespace, persistentvolumeclaim) {_BOUND_PVC_LIMIT}',
                        ),
                        (
                            "B",
                            f'group by (namespace, persistentvolumeclaim, node) (disk_usage:pvc_used_bytes{{node=~"$node"}}) * on (namespace, persistentvolumeclaim) group_left () {_BOUND_PVC_LIMIT}',
                        ),
                        (
                            "C",
                            f'(disk_usage:pvc_used_bytes{{node=~"$node"}} and on (namespace, persistentvolumeclaim) {_BOUND_PVC_LIMIT}) / on (namespace, persistentvolumeclaim) group_left() {_BOUND_PVC_LIMIT}',
                        ),
                    )
                ],
                title="Kubelet filesystem use and Bound Piraeus PVC limit",
                description="Kubelet filesystem-used bytes and volume capacity limits for measured Bound Piraeus PVCs on the selected consumer nodes. Limits are deduplicated across diskful copies. Raw block PVCs and unmounted volumes have no filesystem-used sample; their limits remain visible in the diskful copy table above.",
                datasource=mimir(),
                grid_pos=dashboard.GridPos(h=11, w=24, y=63),
                transformations=[
                    *_table_series("ABC", ("namespace", "persistentvolumeclaim", "node")),
                    dashboard.DataTransformerConfig(id_val="merge", options={}),
                    dashboard.DataTransformerConfig(
                        id_val="organize",
                        options={
                            "excludeByName": {"Time": True},
                            "indexByName": {
                                name: index
                                for index, name in enumerate(
                                    ("namespace", "persistentvolumeclaim", "node", "Value #A", "Value #B", "Value #C")
                                )
                            },
                            "renameByName": {
                                "namespace": "Namespace",
                                "persistentvolumeclaim": "PVC",
                                "node": "Consumer node",
                                "Value #A": "Filesystem used",
                                "Value #B": "Bound PV capacity limit",
                                "Value #C": "Filesystem used / PV limit",
                            },
                        },
                    ),
                    dashboard.DataTransformerConfig(
                        id_val="sortBy", options={"fields": {}, "sort": [{"field": "Filesystem used", "desc": True}]}
                    ),
                ],
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(unit="bytes"),
                    overrides=[field_override("Filesystem used / PV limit", {"unit": "percentunit", "decimals": 1})],
                ),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="storage_pool",
                    label="Storage pool",
                    query="label_values(disk_usage:pool_capacity_bytes, storage_pool)",
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition="label_values(disk_usage:pool_capacity_bytes, storage_pool)",
                ),
                dashboard.VariableModel(
                    name="node",
                    label="Machine",
                    query="label_values(kube_node_info, node)",
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition="label_values(kube_node_info, node)",
                ),
                dashboard.VariableModel(
                    name="pool_node",
                    hide=dashboard.VariableHide.HIDE_VARIABLE,
                    query=f"label_values({_pool_metric('capacity')}, node)",
                    datasource=mimir(),
                    current=dashboard.VariableOption(text="All", value=["$__all"]),
                    multi=True,
                    refresh=dashboard.VariableRefresh.ON_DASHBOARD_LOAD,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    definition=f"label_values({_pool_metric('capacity')}, node)",
                ),
            ]
        ),
    )
