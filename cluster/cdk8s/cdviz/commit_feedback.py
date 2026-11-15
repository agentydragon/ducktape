"""Commit to CI feedback dashboard."""

from textwrap import dedent

from grafana_foundation_sdk.models import common, dashboard, text

from cluster.cdk8s.monitoring.dashboards.foundation import field_override, postgres_query, series_options

_FEEDBACK_CTE = dedent("""\
        WITH feedback AS (
          SELECT *
          FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
          WHERE (
            ${scope:sqlstring} = 'all'
            OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
            OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
          )
            AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
        )""").strip("\n")


def _feedback_query(body: str, *, ctes: str = "") -> str:
    select = dedent(body).strip("\n")
    extra_ctes = dedent(ctes).strip("\n")
    return f"{_FEEDBACK_CTE}{extra_ctes}\n{select}"


def _column(
    name: str, title: str, *, unit: str = "short", decimals: int = 0
) -> dashboard.DashboardFieldConfigSourceOverrides:
    return field_override(name, {"unit": unit, "decimals": decimals, "displayName": title})


def _table_fields(overrides: list[dashboard.DashboardFieldConfigSourceOverrides]) -> dashboard.FieldConfigSource:
    return dashboard.FieldConfigSource(
        defaults=dashboard.FieldConfig(
            unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
        ),
        overrides=overrides,
    )


def _latency_columns() -> list[dashboard.DashboardFieldConfigSourceOverrides]:
    return [
        _column("n", "Heads (N)"),
        *[_column(f"p{quantile}_seconds", f"P{quantile}", unit="s", decimals=1) for quantile in (50, 90, 99)],
    ]


def build_dashboard() -> dashboard.Dashboard:
    return dashboard.Dashboard(
        uid="ci-commit-feedback",
        title="Commit to CI feedback",
        description="Exact-head PR feedback clocks, required-gate policy replay, and censored outcomes. Completion timestamps are not check-query or agent-delivery timestamps.",
        tags=["ci", "ci-feedback", "github-actions"],
        timezone="utc",
        editable=False,
        time=dashboard.DashboardDashboardTime(from_val="now-24h"),
        refresh="1m",
        version=1,
        panels=[
            dashboard.Panel(
                type_val="text",
                id_val=1,
                title="How to read these clocks",
                grid_pos=dashboard.GridPos(h=6, w=24),
                options=text.Options(
                    content="Each row is one head occurrence in the arrival cohort. **PR head event** starts at GitHub\u2019s PR opening or head-change timestamp: a proxy, not the commit\u2019s actual push time. **Workflow creation proxy** starts at the first observed workflow. Select one clock for comparable percentiles.\n\nResults use job completion timestamps, before webhook or agent delivery. Green replays the **latest observed required-check policy**; this is not historical merge eligibility. Missing results remain pending or unknown. Timed rates include censored heads in their denominators; younger heads are still observing. Blank timings mean no sample."
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=2,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  COUNT(*) FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS n,
                                  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p50_seconds,
                                  PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p90_seconds,
                                  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p99_seconds
                                FROM feedback
                                """
                        ),
                        ref_id="feedback_2",
                    )
                ],
                title="First verdict latency",
                description="P50/P90/P99 from arrival to the first red or green verdict. N counts only heads with a verdict; null quantiles with N=0 are intentionally blank, not zero.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=5, y=6),
                field_config=_table_fields(_latency_columns()),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=3,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  verdicts.verdict,
                                  COUNT(latencies.seconds) AS n,
                                  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latencies.seconds) AS p50_seconds,
                                  PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latencies.seconds) AS p90_seconds
                                FROM verdicts
                                LEFT JOIN latencies
                                  ON latencies.verdict = verdicts.verdict
                                  AND latencies.seconds IS NOT NULL
                                GROUP BY verdicts.verdict
                                ORDER BY CASE verdicts.verdict WHEN 'red' THEN 1 ELSE 2 END
                                """,
                            ctes="""\
                                , verdicts(verdict) AS (VALUES ('red'::text), ('green'::text)), latencies AS (
                                  SELECT samples.verdict, samples.seconds
                                  FROM feedback
                                  CROSS JOIN LATERAL (VALUES
                                    ('red'::text, feedback.red_seconds),
                                    ('green'::text, feedback.green_seconds)
                                  ) AS samples(verdict, seconds)
                                )
                                """,
                        ),
                        ref_id="feedback_3",
                    )
                ],
                title="First red vs first green",
                description="Independent first-red and first-green clocks, so a recovered head contributes to both. N counts heads with that observed timestamp; absent samples stay blank.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=5, x=12, y=6),
                field_config=_table_fields(
                    [
                        _column("n", "Heads (N)"),
                        _column("p50_seconds", "P50", unit="s", decimals=1),
                        _column("p90_seconds", "P90", unit="s", decimals=1),
                        field_override("verdict", {"displayName": "Verdict type"}),
                    ]
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  GREATEST(DATE_BIN(${bucket:sqlstring}::interval, arrived_at, TIMESTAMPTZ '2000-01-03 00:00:00+00'), $__timeFrom()::timestamptz) AS time,
                                  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict = 'red' AND first_verdict_seconds IS NOT NULL) AS "Red p50",
                                  PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict = 'red' AND first_verdict_seconds IS NOT NULL) AS "Red p90",
                                  PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict = 'green' AND first_verdict_seconds IS NOT NULL) AS "Green p50",
                                  PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                    FILTER (WHERE first_verdict = 'green' AND first_verdict_seconds IS NOT NULL) AS "Green p90"
                                FROM feedback
                                WHERE first_verdict_seconds IS NOT NULL
                                GROUP BY 1
                                ORDER BY 1
                                """
                        ),
                        ref_id="feedback_4",
                        format="time_series",
                    )
                ],
                title="First-verdict latency (${bucket:text} buckets)",
                description="Arrival cohorts grouped by Bucket size, aligned to UTC midnight with weeks beginning Monday. Only observed red/green verdicts contribute; missing buckets mean no samples, not zero latency. The first partial bucket is plotted at the range start so large buckets remain visible.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(w=16, y=11),
                options=series_options(sort=common.SortOrder.ASCENDING),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        decimals=1,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, show_points=common.VisibilityMode.AUTO
                        ),
                    ),
                    overrides=[
                        field_override("Red p50", {"color": {"mode": "fixed", "fixedColor": "#F2495C"}}),
                        field_override("Red p90", {"color": {"mode": "fixed", "fixedColor": "#FF9830"}}),
                        field_override("Green p50", {"color": {"mode": "fixed", "fixedColor": "#73BF69"}}),
                        field_override("Green p90", {"color": {"mode": "fixed", "fixedColor": "#56A64B"}}),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=5,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT state, COUNT(*) AS heads
                                FROM feedback
                                GROUP BY state
                                ORDER BY CASE state
                                  WHEN 'green' THEN 1
                                  WHEN 'red' THEN 2
                                  WHEN 'pending' THEN 3
                                  WHEN 'cancelled' THEN 4
                                  WHEN 'superseded' THEN 5
                                  ELSE 6
                                END
                                """
                        ),
                        ref_id="feedback_5",
                    )
                ],
                title="Head outcomes",
                description="Counts by exact-head state in the arrival cohort. Canceled and superseded are separate from red/green verdicts; pending and unknown remain visible.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(w=8, x=16, y=11),
                field_config=_table_fields(
                    [_column("heads", "Heads"), field_override("state", {"displayName": "Outcome"})]
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=6,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  limits.label,
                                  COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                  ) AS eligible_heads,
                                  COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at < limits.seconds * INTERVAL '1 second'
                                  ) AS still_observing,
                                  COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                      AND feedback.first_verdict IN ('red', 'green')
                                      AND feedback.first_verdict_seconds <= limits.seconds
                                  ) AS heads_with_verdict,
                                  100.0 * COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                      AND feedback.first_verdict IN ('red', 'green')
                                      AND feedback.first_verdict_seconds <= limits.seconds
                                  ) / NULLIF(COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                  ), 0) AS verdict_rate_percent,
                                  COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                      AND feedback.green_seconds <= limits.seconds
                                  ) AS heads_green,
                                  100.0 * COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                      AND feedback.green_seconds <= limits.seconds
                                  ) / NULLIF(COUNT(*) FILTER (
                                    WHERE $__timeTo()::timestamptz - feedback.arrived_at >= limits.seconds * INTERVAL '1 second'
                                  ), 0) AS green_rate_percent
                                FROM feedback
                                CROSS JOIN limits
                                GROUP BY limits.label, limits.seconds
                                ORDER BY limits.seconds
                                """,
                            ctes="""\
                                , limits(label, seconds) AS (
                                  VALUES ('5 minutes'::text, 300), ('10 minutes'::text, 600), ('20 minutes'::text, 1200)
                                )
                                """,
                        ),
                        ref_id="feedback_6",
                    )
                ],
                title="Verdict / green within 5 / 10 / 20 minutes",
                description="Both rates use every head at least the threshold old as of the dashboard end, including censored heads. Still-observing counts arrivals younger than that threshold. Verdict counts first red or green; green counts heads with observed all-required-gates green. Empty denominators have blank rates, not 0%.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=6, w=24, y=20),
                field_config=_table_fields(
                    [
                        field_override(
                            "eligible_heads",
                            {"unit": "short", "decimals": 0, "displayName": "Eligible", "custom.width": 120},
                        ),
                        field_override(
                            "heads_with_verdict",
                            {
                                "unit": "short",
                                "decimals": 0,
                                "displayName": "Heads with verdict",
                                "custom.hidden": True,
                            },
                        ),
                        field_override(
                            "verdict_rate_percent",
                            {"unit": "percent", "decimals": 0, "displayName": "Verdict %", "custom.width": 120},
                        ),
                        field_override(
                            "heads_green",
                            {"unit": "short", "decimals": 0, "displayName": "Heads green", "custom.hidden": True},
                        ),
                        field_override(
                            "green_rate_percent",
                            {"unit": "percent", "decimals": 0, "displayName": "Green %", "custom.width": 120},
                        ),
                        field_override("label", {"displayName": "Window", "custom.width": 130}),
                        field_override(
                            "still_observing",
                            {"displayName": "Still observing", "unit": "short", "decimals": 0, "custom.width": 150},
                        ),
                    ]
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=7,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  COUNT(*) AS heads,
                                  MAX(policy_observed_at) AS latest_observed_policy_replay,
                                  COUNT(*) FILTER (WHERE policy_observed_at IS NOT NULL) AS policy_snapshot_heads,
                                  COUNT(*) FILTER (
                                    WHERE policy_observed_at IS NOT NULL
                                      AND required_gates > 0
                                      AND observed_gates = required_gates
                                  ) AS fully_observed_gate_heads,
                                  COUNT(*) FILTER (
                                    WHERE missing_gates IS NOT NULL AND BTRIM(missing_gates) <> ''
                                  ) AS heads_with_named_missing_gates,
                                  COUNT(*) FILTER (WHERE policy_observed_at IS NULL) AS heads_without_policy
                                FROM feedback
                                """
                        ),
                        ref_id="feedback_7",
                    )
                ],
                title="Required-policy replay coverage",
                description="Latest observed policy replay is taken as of the selected dashboard end; the timestamp is shown in the table. Coverage counts do not establish historical eligibility, and equal gate counts alone do not mean a head is green. The recent-head table retains exact missing gate names and per-head policy observation time.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=24, y=26),
                field_config=_table_fields(
                    [
                        field_override(
                            "heads", {"unit": "short", "decimals": 0, "displayName": "Heads", "custom.width": 85}
                        ),
                        field_override(
                            "policy_snapshot_heads",
                            {
                                "unit": "short",
                                "decimals": 0,
                                "displayName": "Heads with policy snapshot",
                                "custom.hidden": True,
                            },
                        ),
                        field_override(
                            "fully_observed_gate_heads",
                            {"unit": "short", "decimals": 0, "displayName": "All gates observed", "custom.width": 145},
                        ),
                        field_override(
                            "heads_with_named_missing_gates",
                            {"unit": "short", "decimals": 0, "displayName": "Missing results", "custom.width": 145},
                        ),
                        field_override(
                            "latest_observed_policy_replay",
                            {"unit": "dateTimeAsIso", "displayName": "Policy observed at", "custom.width": 245},
                        ),
                        field_override(
                            "heads_without_policy",
                            {"displayName": "No policy snapshot", "unit": "short", "decimals": 0, "custom.width": 135},
                        ),
                    ]
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    postgres_query(
                        sql=_feedback_query(
                            """\
                                SELECT
                                  arrived_at AS "Arrived",
                                  pr AS "PR",
                                  LEFT(head_sha, 10) AS "Head SHA",
                                  state AS "State",
                                  first_verdict AS "First verdict",
                                  ROUND(first_verdict_seconds::numeric, 1) AS "Verdict seconds",
                                  ROUND(red_seconds::numeric, 1) AS "Red seconds",
                                  ROUND(green_seconds::numeric, 1) AS "Green seconds",
                                  observed_gates::text || '/' || required_gates::text AS "Gates",
                                  clock AS "Arrival clock",
                                  missing_gates AS "Missing gates",
                                  repository AS "Repository",
                                  url AS "URL"
                                FROM feedback
                                ORDER BY arrived_at DESC
                                LIMIT 50
                                """
                        ),
                        ref_id="feedback_8",
                    )
                ],
                title="Recent exact-head feedback",
                description="One row per head, newest arrival first. Null timing fields indicate that verdict has not been observed; the URL points to the PR or commit when available.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=10, w=24, y=30),
                field_config=_table_fields(
                    [
                        field_override(
                            "URL",
                            {"links": [{"title": "Open PR or commit", "url": "${__value.raw}"}], "custom.hidden": True},
                        ),
                        field_override(
                            "Head SHA",
                            {
                                "custom.width": 90,
                                "unit": "string",
                                "decimals": 0,
                                "links": [
                                    {"title": "Open commit", "url": '${__data.fields["URL"]}', "targetBlank": True}
                                ],
                                "displayName": "Commit",
                            },
                        ),
                        field_override("Missing gates", {"custom.width": 105, "displayName": "Missing"}),
                        field_override("Repository", {"custom.hidden": True}),
                        field_override(
                            "PR",
                            {
                                "unit": "short",
                                "decimals": 0,
                                "custom.width": 45,
                                "links": [
                                    {
                                        "title": "Open pull request",
                                        "url": 'https://github.com/${__data.fields["Repository"]}/pull/${__value.raw}',
                                        "targetBlank": True,
                                    }
                                ],
                            },
                        ),
                        field_override("Arrived", {"displayName": "Arrived", "custom.width": 170}),
                        field_override(
                            "State",
                            {
                                "displayName": "State",
                                "custom.width": 65,
                                "custom.cellOptions": {"type": "color-text"},
                                "mappings": [
                                    {
                                        "type": "value",
                                        "options": {
                                            "green": {"text": "green", "color": "green"},
                                            "red": {"text": "red", "color": "red"},
                                            "pending": {"text": "pending", "color": "yellow"},
                                            "cancelled": {"text": "cancelled", "color": "gray"},
                                            "superseded": {"text": "superseded", "color": "purple"},
                                            "unknown": {"text": "unknown", "color": "gray"},
                                        },
                                    }
                                ],
                            },
                        ),
                        field_override("First verdict", {"displayName": "First verdict", "custom.width": 90}),
                        field_override(
                            "Verdict seconds",
                            {"displayName": "Feedback", "custom.width": 85, "unit": "s", "decimals": 1},
                        ),
                        field_override(
                            "Red seconds",
                            {"displayName": "Red latency", "custom.width": 70, "unit": "s", "decimals": 1},
                        ),
                        field_override(
                            "Green seconds",
                            {"displayName": "Green latency", "custom.width": 75, "unit": "s", "decimals": 1},
                        ),
                        field_override("Arrival clock", {"displayName": "Clock", "custom.width": 80}),
                        field_override("Gates", {"unit": "string", "custom.width": 55, "displayName": "Seen/req"}),
                    ]
                ),
            ),
        ],
        templating=dashboard.DashboardDashboardTemplating(
            list_val=[
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="scope",
                    label="Scope",
                    hide=dashboard.VariableHide.DONT_HIDE,
                    description="PR includes heads with an associated PR number; devel includes unassociated devel/main branch heads; all also includes unknown PR associations.",
                    query="PR, devel, all",
                    current=dashboard.VariableOption(text="PR", value="PR"),
                    options=[
                        dashboard.VariableOption(selected=True, text="PR", value="PR"),
                        dashboard.VariableOption(selected=False, text="devel", value="devel"),
                        dashboard.VariableOption(selected=False, text="all", value="all"),
                    ],
                ),
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="clock",
                    label="Clock",
                    hide=dashboard.VariableHide.DONT_HIDE,
                    description="Default to the observed PR head event clock. Choose Workflow creation proxy for fallback-only comparisons; all pools both clocks and is a descriptive mixed-clock view.",
                    query="PR head event, Workflow creation proxy, all",
                    current=dashboard.VariableOption(text="PR head event", value="PR head event"),
                    options=[
                        dashboard.VariableOption(selected=True, text="PR head event", value="PR head event"),
                        dashboard.VariableOption(
                            selected=False, text="Workflow creation proxy", value="Workflow creation proxy"
                        ),
                        dashboard.VariableOption(selected=False, text="all", value="all"),
                    ],
                ),
                dashboard.VariableModel(
                    type_val=dashboard.VariableType.CUSTOM,
                    name="bucket",
                    label="Bucket size",
                    hide=dashboard.VariableHide.DONT_HIDE,
                    description="Arrival-cohort size for the latency trend. Other panels use the full selected time range.",
                    query="1m : 1 minute, 5m : 5 minutes, 15m : 15 minutes, 30m : 30 minutes, 1h : 1 hour, 3h : 3 hours, 6h : 6 hours, 12h : 12 hours, 1d : 1 day, 1w : 1 week",
                    current=dashboard.VariableOption(text="1h", value="1 hour"),
                    allow_custom_value=False,
                    options=[
                        dashboard.VariableOption(selected=False, text="1m", value="1 minute"),
                        dashboard.VariableOption(selected=False, text="5m", value="5 minutes"),
                        dashboard.VariableOption(selected=False, text="15m", value="15 minutes"),
                        dashboard.VariableOption(selected=False, text="30m", value="30 minutes"),
                        dashboard.VariableOption(selected=True, text="1h", value="1 hour"),
                        dashboard.VariableOption(selected=False, text="3h", value="3 hours"),
                        dashboard.VariableOption(selected=False, text="6h", value="6 hours"),
                        dashboard.VariableOption(selected=False, text="12h", value="12 hours"),
                        dashboard.VariableOption(selected=False, text="1d", value="1 day"),
                        dashboard.VariableOption(selected=False, text="1w", value="1 week"),
                    ],
                ),
            ]
        ),
    )
