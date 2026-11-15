"""Commit to CI feedback dashboard."""

from textwrap import dedent

from grafana_foundation_sdk.models import common, dashboard, text, timeseries

from cluster.cdk8s.monitoring.dashboards.foundation import postgres_query


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
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            )
                            SELECT
                              COUNT(*) FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS n,
                              PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p50_seconds,
                              PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p90_seconds,
                              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY first_verdict_seconds)
                                FILTER (WHERE first_verdict IN ('red', 'green') AND first_verdict_seconds IS NOT NULL) AS p99_seconds
                            FROM feedback""").strip("\n"),
                        ref_id="feedback_2",
                    )
                ],
                title="First verdict latency",
                description="P50/P90/P99 from arrival to the first red or green verdict. N counts only heads with a verdict; null quantiles with N=0 are intentionally blank, not zero.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=5, y=6),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="n"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads (N)"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="p50_seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                                dashboard.DynamicConfigValue(id_val="displayName", value="P50"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="p90_seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                                dashboard.DynamicConfigValue(id_val="displayName", value="P90"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="p99_seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                                dashboard.DynamicConfigValue(id_val="displayName", value="P99"),
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=3,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            ), verdicts(verdict) AS (VALUES ('red'::text), ('green'::text)), latencies AS (
                              SELECT samples.verdict, samples.seconds
                              FROM feedback
                              CROSS JOIN LATERAL (VALUES
                                ('red'::text, feedback.red_seconds),
                                ('green'::text, feedback.green_seconds)
                              ) AS samples(verdict, seconds)
                            )
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
                            ORDER BY CASE verdicts.verdict WHEN 'red' THEN 1 ELSE 2 END""").strip("\n"),
                        ref_id="feedback_3",
                    )
                ],
                title="First red vs first green",
                description="Independent first-red and first-green clocks, so a recovered head contributes to both. N counts heads with that observed timestamp; absent samples stay blank.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=5, x=12, y=6),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="n"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads (N)"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="p50_seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                                dashboard.DynamicConfigValue(id_val="displayName", value="P50"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="p90_seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                                dashboard.DynamicConfigValue(id_val="displayName", value="P90"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="verdict"),
                            properties=[dashboard.DynamicConfigValue(id_val="displayName", value="Verdict type")],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="timeseries",
                id_val=4,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            )
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
                            ORDER BY 1""").strip("\n"),
                        ref_id="feedback_4",
                        format="time_series",
                    )
                ],
                title="First-verdict latency (${bucket:text} buckets)",
                description="Arrival cohorts grouped by Bucket size, aligned to UTC midnight with weeks beginning Monday. Only observed red/green verdicts contribute; missing buckets mean no samples, not zero latency. The first partial bucket is plotted at the range start so large buckets remain visible.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(w=16, y=11),
                options=timeseries.Options(
                    legend=common.VizLegendOptions(show_legend=True),
                    tooltip=common.VizTooltipOptions(mode=common.TooltipDisplayMode.MULTI),
                ),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="s",
                        decimals=1,
                        custom=common.GraphFieldConfig(
                            draw_style=common.GraphDrawStyle.LINE, line_width=2, show_points=common.VisibilityMode.AUTO
                        ),
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Red p50"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="color", value={"mode": "fixed", "fixedColor": "#F2495C"}
                                )
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Red p90"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="color", value={"mode": "fixed", "fixedColor": "#FF9830"}
                                )
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Green p50"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="color", value={"mode": "fixed", "fixedColor": "#73BF69"}
                                )
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Green p90"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="color", value={"mode": "fixed", "fixedColor": "#56A64B"}
                                )
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=5,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            )
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
                            END""").strip("\n"),
                        ref_id="feedback_5",
                    )
                ],
                title="Head outcomes",
                description="Counts by exact-head state in the arrival cohort. Canceled and superseded are separate from red/green verdicts; pending and unknown remain visible.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(w=8, x=16, y=11),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="state"),
                            properties=[dashboard.DynamicConfigValue(id_val="displayName", value="Outcome")],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=6,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            ), limits(label, seconds) AS (
                              VALUES ('5 minutes'::text, 300), ('10 minutes'::text, 600), ('20 minutes'::text, 1200)
                            )
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
                            ORDER BY limits.seconds""").strip("\n"),
                        ref_id="feedback_6",
                    )
                ],
                title="Verdict / green within 5 / 10 / 20 minutes",
                description="Both rates use every head at least the threshold old as of the dashboard end, including censored heads. Still-observing counts arrivals younger than that threshold. Verdict counts first red or green; green counts heads with observed all-required-gates green. Empty denominators have blank rates, not 0%.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=6, w=24, y=20),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="eligible_heads"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Eligible"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=120),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads_with_verdict"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads with verdict"),
                                dashboard.DynamicConfigValue(id_val="custom.hidden", value=True),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="verdict_rate_percent"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="percent"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Verdict %"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=120),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads_green"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads green"),
                                dashboard.DynamicConfigValue(id_val="custom.hidden", value=True),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="green_rate_percent"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="percent"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Green %"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=120),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="label"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Window"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=130),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="still_observing"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Still observing"),
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=150),
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=7,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            )
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
                            FROM feedback""").strip("\n"),
                        ref_id="feedback_7",
                    )
                ],
                title="Required-policy replay coverage",
                description="Latest observed policy replay is taken as of the selected dashboard end; the timestamp is shown in the table. Coverage counts do not establish historical eligibility, and equal gate counts alone do not mean a head is green. The recent-head table retains exact missing gate names and per-head policy observation time.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=4, w=24, y=26),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=85),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="policy_snapshot_heads"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Heads with policy snapshot"),
                                dashboard.DynamicConfigValue(id_val="custom.hidden", value=True),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="fully_observed_gate_heads"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="All gates observed"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=145),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads_with_named_missing_gates"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Missing results"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=145),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="latest_observed_policy_replay"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="dateTimeAsIso"),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Policy observed at"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=245),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="heads_without_policy"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="No policy snapshot"),
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=135),
                            ],
                        ),
                    ],
                ),
            ),
            dashboard.Panel(
                type_val="table",
                id_val=8,
                targets=[
                    postgres_query(
                        sql=dedent("""\

                            WITH feedback AS (
                              SELECT *
                              FROM cdviz.ci_feedback($__timeFrom(), $__timeTo())
                              WHERE (
                                ${scope:sqlstring} = 'all'
                                OR (${scope:sqlstring} = 'PR' AND pr IS NOT NULL)
                                OR (${scope:sqlstring} = 'devel' AND pr IS NULL AND branch IN ('devel', 'main'))
                              )
                                AND (${clock:sqlstring} = 'all' OR clock = ${clock:sqlstring})
                            )
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
                            LIMIT 50""").strip("\n"),
                        ref_id="feedback_8",
                    )
                ],
                title="Recent exact-head feedback",
                description="One row per head, newest arrival first. Null timing fields indicate that verdict has not been observed; the URL points to the PR or commit when available.",
                datasource=common.DataSourceRef(type_val="grafana-postgresql-datasource", uid="cdviz"),
                grid_pos=dashboard.GridPos(h=10, w=24, y=30),
                field_config=dashboard.FieldConfigSource(
                    defaults=dashboard.FieldConfig(
                        unit="short", decimals=1, custom=common.TableFieldOptions(width=120, min_width=80)
                    ),
                    overrides=[
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="URL"),
                            properties=[
                                dashboard.DynamicConfigValue(
                                    id_val="links", value=[{"title": "Open PR or commit", "url": "${__value.raw}"}]
                                ),
                                dashboard.DynamicConfigValue(id_val="custom.hidden", value=True),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Head SHA"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="custom.width", value=90),
                                dashboard.DynamicConfigValue(id_val="unit", value="string"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(
                                    id_val="links",
                                    value=[
                                        {"title": "Open commit", "url": '${__data.fields["URL"]}', "targetBlank": True}
                                    ],
                                ),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Commit"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Missing gates"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="custom.width", value=105),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Missing"),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Repository"),
                            properties=[dashboard.DynamicConfigValue(id_val="custom.hidden", value=True)],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="PR"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="short"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=0),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=45),
                                dashboard.DynamicConfigValue(
                                    id_val="links",
                                    value=[
                                        {
                                            "title": "Open pull request",
                                            "url": 'https://github.com/${__data.fields["Repository"]}/pull/${__value.raw}',
                                            "targetBlank": True,
                                        }
                                    ],
                                ),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Arrived"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Arrived"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=170),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="State"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="State"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=65),
                                dashboard.DynamicConfigValue(id_val="custom.cellOptions", value={"type": "color-text"}),
                                dashboard.DynamicConfigValue(
                                    id_val="mappings",
                                    value=[
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
                                ),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="First verdict"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="First verdict"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=90),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Verdict seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Feedback"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=85),
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Red seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Red latency"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=70),
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Green seconds"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Green latency"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=75),
                                dashboard.DynamicConfigValue(id_val="unit", value="s"),
                                dashboard.DynamicConfigValue(id_val="decimals", value=1),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Arrival clock"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="displayName", value="Clock"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=80),
                            ],
                        ),
                        dashboard.DashboardFieldConfigSourceOverrides(
                            matcher=dashboard.MatcherConfig(id_val="byName", options="Gates"),
                            properties=[
                                dashboard.DynamicConfigValue(id_val="unit", value="string"),
                                dashboard.DynamicConfigValue(id_val="custom.width", value=55),
                                dashboard.DynamicConfigValue(id_val="displayName", value="Seen/req"),
                            ],
                        ),
                    ],
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
