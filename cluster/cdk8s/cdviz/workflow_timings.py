"""CI workflow and job timings dashboard."""

from textwrap import dedent

from grafana_foundation_sdk.models import common, dashboard

from cluster.cdk8s.monitoring.dashboards.foundation import field_override, postgres_query, series_options, stat_options

_RUNS_CTE = dedent("""\
        WITH runs AS (
          SELECT p.*,
            COALESCE(p.finished_at, p.started_at, p.queued_at) AS observed_at,
            COALESCE(p.last_payload #>> '{subject,content,pipelineName}', '(unknown)') AS workflow,
            COALESCE(p.last_payload #>> '{customData,github,workflow_run,head_branch}', '(unknown)') AS branch,
            p.last_payload #> '{customData,github,workflow_run,pull_requests}' AS prs
          FROM cdviz.pipelinerun p
        )""").strip("\n")

_JOBS_CTE = dedent("""\
        WITH jobs AS (
          SELECT t.*,
            COALESCE(t.finished_at, t.started_at) AS observed_at,
            COALESCE(p.last_payload #>> '{subject,content,pipelineName}', 'agentydragon/ducktape/' || (t.last_payload #>> '{customData,github,workflow_job,workflow_name}'), '(unknown)') AS workflow,
            COALESCE(t.last_payload #>> '{customData,github,workflow_job,head_branch}', p.last_payload #>> '{customData,github,workflow_run,head_branch}', '(unknown)') AS branch,
            p.last_payload #> '{customData,github,workflow_run,pull_requests}' AS prs
          FROM cdviz.taskrun t
          LEFT JOIN cdviz.pipelinerun p ON p.subject_id = t.last_payload #>> '{subject,content,pipelineRun,id}'
        )""").strip("\n")

_TIMING_FILTER = dedent("""\
        WHERE $__timeFilter(observed_at)
          AND (${workflow:sqlstring} = '__all' OR workflow = ${workflow:sqlstring})
          AND (${branch:sqlstring} = '__all' OR branch = ${branch:sqlstring})
          AND (${pr:sqlstring} = '' OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(COALESCE(prs, '[]'::jsonb)) AS item
            WHERE item ->> 'number' = ${pr:sqlstring}
          ))""").strip("\n")


def _timing_query(cte: str, body: str, tail: str = "") -> str:
    select = dedent(body).strip("\n")
    suffix = "\n" + dedent(tail).strip("\n") if tail else ""
    return f"{cte}\n{select}\n{_TIMING_FILTER}{suffix}"


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="ci-workflow-timings",
        title="CI workflow and job timings",
        description="Workflow attempts and jobs from GitHub Actions. Select a workflow, branch, or explicitly associated PR to inspect runtimes.",
        tags=["ci", "github-actions"],
        timezone="utc",
        editable=False,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="1m",
        version=1,
        panels=[
            dashboard.Panel(
                type_val="stat",
                id_val=1,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT COUNT(*) AS attempts
                                FROM runs
                                """,
                        )
                    )
                ],
                title="Workflow attempts",
                description="Attempts observed in the selected time range, including unfinished attempts.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=6),
                options=stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        decimals=0,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=2,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT COALESCE(100.0 * COUNT(*) FILTER (WHERE outcome IN ('fail', 'failure', 'error')) / NULLIF(COUNT(*) FILTER (WHERE finished_at IS NOT NULL), 0), 0) AS failure_rate
                                FROM runs
                                """,
                        ),
                        ref_id="B",
                    )
                ],
                title="Failure rate",
                description="Failed or errored attempts divided by all finished attempts.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=6, x=6),
                options=stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="percent",
                        decimals=1,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=3,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))) FILTER (WHERE finished_at IS NOT NULL AND started_at IS NOT NULL) AS p50_runtime
                                FROM runs
                                """,
                        ),
                        ref_id="C",
                    )
                ],
                title="P50 workflow runtime",
                description="Median started-to-finished duration of completed workflow attempts.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=6, x=12),
                options=stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        decimals=1,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="stat",
                id_val=4,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))) FILTER (WHERE finished_at IS NOT NULL AND started_at IS NOT NULL) AS p95_runtime
                                FROM runs
                                """,
                        ),
                        ref_id="D",
                    )
                ],
                title="P95 workflow runtime",
                description="95th percentile started-to-finished duration of completed workflow attempts.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=6, x=18),
                options=stat_options(),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        decimals=1,
                        color=dashboard.FieldColor(mode=dashboard.FieldColorModeId.FIXED, fixed_color="blue"),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=5,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT time_bucket('1 hour', finished_at) AS time,
                                  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))) AS "P50",
                                  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))) AS "P95"
                                FROM runs
                                """,
                            """\
                                  AND finished_at IS NOT NULL AND started_at IS NOT NULL
                                GROUP BY 1
                                ORDER BY 1
                                """,
                        ),
                        ref_id="E",
                        format="time_series",
                    )
                ],
                title="Workflow runtime over time",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=7, y=4),
                options=series_options(sort=common.SortOrder.ASCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        decimals=1,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE,
                            line_width=2,
                            show_points=common.VisibilityMode.ALWAYS,
                            point_size=5,
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=6,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT time_bucket('1 hour', observed_at) AS time,
                                  COUNT(*) FILTER (WHERE outcome IN ('pass', 'success')) AS "Success",
                                  COUNT(*) FILTER (WHERE outcome IN ('fail', 'failure', 'error')) AS "Failure",
                                  COUNT(*) FILTER (WHERE outcome IN ('cancel', 'cancelled', 'canceled')) AS "Cancelled",
                                  COUNT(*) FILTER (WHERE finished_at IS NULL AND started_at IS NOT NULL) AS "Running",
                                  COUNT(*) FILTER (WHERE finished_at IS NULL AND started_at IS NULL) AS "Queued"
                                FROM runs
                                """,
                            """\
                                GROUP BY 1
                                ORDER BY 1
                                """,
                        ),
                        ref_id="F",
                        format="time_series",
                    )
                ],
                title="Workflow outcomes over time",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=7, x=12, y=4),
                options=series_options(sort=common.SortOrder.ASCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short",
                        decimals=0,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.BARS,
                            line_width=0,
                            fill_opacity=75,
                            stacking=common.StackingConfig(mode=common.StackingMode.NORMAL, group="outcomes"),
                        ),
                    )
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=7,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _RUNS_CTE,
                            """\
                                SELECT
                                  regexp_replace(workflow, '^agentydragon/ducktape/', '') AS "Workflow",
                                  (SELECT string_agg(item ->> 'number', ', ') FROM jsonb_array_elements(COALESCE(prs, '[]'::jsonb)) AS item) AS "PR",
                                  branch AS "Branch",
                                  last_payload #>> '{customData,github,workflow_run,run_attempt}' AS "Attempt",
                                  CASE WHEN outcome IN ('cancel', 'cancelled', 'canceled') THEN 'cancelled' WHEN finished_at IS NULL AND started_at IS NOT NULL THEN 'running' WHEN finished_at IS NULL THEN 'queued' ELSE COALESCE(outcome, 'unknown') END AS "Outcome",
                                  started_at AS "Started",
                                  ROUND(EXTRACT(EPOCH FROM (started_at - queued_at))::numeric) AS "Queue to start",
                                  ROUND(EXTRACT(EPOCH FROM (finished_at - started_at))::numeric) AS "Runtime",
                                  LEFT(last_payload #>> '{customData,github,workflow_run,head_sha}', 10) AS "Commit",
                                  COALESCE(last_payload #>> '{subject,content,uri}', last_payload #>> '{subject,content,url}') AS "URL"
                                FROM runs
                                """,
                            """\
                                ORDER BY observed_at DESC
                                LIMIT 100
                                """,
                        ),
                        ref_id="G",
                    )
                ],
                title="Recent workflow attempts",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=8, w=24, y=11),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=0, custom=common.TableFieldOptions(width=90, min_width=60)
                    ),
                    overrides=[
                        field_override(
                            "Workflow",
                            {
                                "links": [
                                    {"title": "Open in GitHub", "url": '${__data.fields["URL"]}', "targetBlank": True}
                                ],
                                "custom.width": 250,
                            },
                        ),
                        field_override("URL", {"custom.hidden": True}),
                        field_override("Runtime", {"unit": "s", "decimals": 0}),
                        field_override("Queue to start", {"unit": "s", "decimals": 0, "custom.width": 110}),
                        field_override("Started", {"unit": "dateTimeAsIso", "custom.width": 170}),
                        field_override("Commit", {"custom.hidden": True}),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    postgres_query(
                        sql=_timing_query(
                            _JOBS_CTE,
                            """\
                                SELECT
                                  COALESCE(last_payload #>> '{customData,github,workflow_job,name}', last_payload #>> '{subject,content,taskName}') AS "Job",
                                  regexp_replace(workflow, '^agentydragon/ducktape/', '') AS "Workflow",
                                  branch AS "Branch",
                                  last_payload #>> '{customData,github,workflow_job,run_attempt}' AS "Attempt",
                                  CASE WHEN outcome IN ('cancel', 'cancelled', 'canceled') THEN 'cancelled' WHEN finished_at IS NULL AND started_at IS NOT NULL THEN 'running' WHEN finished_at IS NULL THEN 'queued' ELSE COALESCE(outcome, 'unknown') END AS "Outcome",
                                  started_at AS "Started",
                                  ROUND(EXTRACT(EPOCH FROM (finished_at - started_at))::numeric) AS "Runtime",
                                  LEFT(last_payload #>> '{customData,github,workflow_job,head_sha}', 10) AS "Commit",
                                  COALESCE(last_payload #>> '{subject,content,uri}', last_payload #>> '{subject,content,url}') AS "URL"
                                FROM jobs
                                """,
                            """\
                                ORDER BY observed_at DESC
                                LIMIT 100
                                """,
                        ),
                        ref_id="H",
                    )
                ],
                title="Recent jobs",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=8, w=24, y=19),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=0, custom=common.TableFieldOptions(width=90, min_width=60)
                    ),
                    overrides=[
                        field_override(
                            "Job",
                            {
                                "links": [
                                    {"title": "Open in GitHub", "url": '${__data.fields["URL"]}', "targetBlank": True}
                                ],
                                "custom.width": 200,
                            },
                        ),
                        field_override("URL", {"custom.hidden": True}),
                        field_override("Runtime", {"unit": "s", "decimals": 0}),
                        field_override("Queue to start", {"unit": "s", "decimals": 0}),
                        field_override("Started", {"unit": "dateTimeAsIso", "custom.width": 170}),
                        field_override("Workflow", {"custom.width": 240}),
                        field_override("Commit", {"custom.hidden": True}),
                    ],
                ),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    name="workflow",
                    label="Workflow",
                    query="WITH runs AS (\n  SELECT p.*,\n    COALESCE(p.finished_at, p.started_at, p.queued_at) AS observed_at,\n    COALESCE(p.last_payload #>> '{subject,content,pipelineName}', '(unknown)') AS workflow,\n    COALESCE(p.last_payload #>> '{customData,github,workflow_run,head_branch}', '(unknown)') AS branch,\n    p.last_payload #> '{customData,github,workflow_run,pull_requests}' AS prs\n  FROM cdviz.pipelinerun p\n)\nSELECT DISTINCT workflow AS __value, regexp_replace(workflow, '^agentydragon/ducktape/', '') AS __text FROM runs WHERE $__timeFilter(observed_at) ORDER BY __text",
                    datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                    current=dashboard.VariableOption(text="All", value="$__all"),
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value="'__all'",
                ),
                dashboard.VariableModel(
                    name="branch",
                    label="Branch",
                    query="WITH runs AS (\n  SELECT p.*,\n    COALESCE(p.finished_at, p.started_at, p.queued_at) AS observed_at,\n    COALESCE(p.last_payload #>> '{subject,content,pipelineName}', '(unknown)') AS workflow,\n    COALESCE(p.last_payload #>> '{customData,github,workflow_run,head_branch}', '(unknown)') AS branch,\n    p.last_payload #> '{customData,github,workflow_run,pull_requests}' AS prs\n  FROM cdviz.pipelinerun p\n)\nSELECT DISTINCT branch AS __value FROM runs WHERE $__timeFilter(observed_at) ORDER BY __value",
                    datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                    current=dashboard.VariableOption(text="All", value="$__all"),
                    refresh=dashboard.VariableRefresh.ON_TIME_RANGE_CHANGED,
                    sort=dashboard.VariableSort.ALPHABETICAL_ASC,
                    include_all=True,
                    all_value="'__all'",
                ),
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.TEXTBOX,
                    name="pr",
                    label="PR number",
                    query="",
                    current=dashboard.VariableOption(),
                ),
            ]
        ),
    )
