-- Additive analytics migration. Source clocks and raw conclusions deliberately
-- bypass the upstream aggregate views, which discard transitions and map skipped
-- checks to cancellation. Later schema changes belong in a new migration.
CREATE OR REPLACE FUNCTION cdviz.ci_feedback(p_from timestamptz, p_to timestamptz)
RETURNS TABLE (
    repository text, head_sha text, pr text, branch text,
    arrived_at timestamptz, clock text, superseded_at timestamptz,
    first_red_at timestamptz, first_green_at timestamptz, first_verdict text,
    first_verdict_seconds numeric, red_seconds numeric, green_seconds numeric,
    state text, observed_gates integer, required_gates integer, missing_gates text,
    policy_observed_at timestamptz, url text
)
LANGUAGE sql STABLE AS $$
WITH lake AS MATERIALIZED (
    SELECT timestamp AS event_at, predicate, payload,
        payload #> '{customData,github}' AS github,
        COALESCE(payload #>> '{customData,github,repository,full_name}',
            split_part(payload #>> '{subject,content,pipelineName}', '/', 1) || '/' ||
            split_part(payload #>> '{subject,content,pipelineName}', '/', 2)) AS repo
    FROM cdviz.cdevents_lake
    WHERE timestamp >= p_from - interval '1 day' AND timestamp <= p_to
), policy AS (
    -- This is an explicitly labeled replay under the latest observed policy,
    -- not a claim about which rules GitHub enforced at a historical instant.
    SELECT payload #> '{customData,ci_policy}' AS data, timestamp AS observed_at
    FROM cdviz.cdevents_lake
    WHERE timestamp <= p_to AND payload #> '{customData,ci_policy}' IS NOT NULL
    ORDER BY timestamp DESC LIMIT 1
), required AS (
    SELECT DISTINCT jsonb_array_elements_text(data -> 'contexts') AS name FROM policy
), pr_events AS (
    SELECT DISTINCT repo, github #>> '{pull_request,head,sha}' AS sha,
        github #>> '{pull_request,number}' AS pr_number,
        github #>> '{pull_request,head,ref}' AS head_branch,
        CASE WHEN github ->> 'action' = 'opened'
            THEN (github #>> '{pull_request,created_at}')::timestamptz
            ELSE (github #>> '{pull_request,updated_at}')::timestamptz END AS at
    FROM lake
    WHERE github ->> 'action' IN ('opened', 'synchronize')
        AND github #>> '{pull_request,head,sha}' IS NOT NULL
), pr_changes AS (
    SELECT *, lag(sha) OVER (PARTITION BY repo, pr_number ORDER BY at, sha) AS previous_sha
    FROM pr_events
), pr_heads AS (
    SELECT repo, sha, pr_number, head_branch, at, 'PR head event' AS start_clock,
        lead(at) OVER (PARTITION BY repo, pr_number ORDER BY at, sha) AS replaced_at
    FROM pr_changes WHERE sha IS DISTINCT FROM previous_sha
), runs AS (
    SELECT *, github -> 'workflow_run' AS run FROM lake
    WHERE github -> 'workflow_run' IS NOT NULL
), fallback_heads AS (
    SELECT r.repo, r.run ->> 'head_sha' AS sha, item ->> 'number' AS pr_number,
        r.run ->> 'head_branch' AS head_branch,
        min((r.run ->> 'created_at')::timestamptz) AS at,
        'Workflow creation proxy' AS start_clock, NULL::timestamptz AS replaced_at
    FROM runs r
    LEFT JOIN LATERAL jsonb_array_elements(r.run -> 'pull_requests') item ON true
    WHERE r.run ->> 'created_at' IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM pr_heads h WHERE h.repo = r.repo AND h.sha = r.run ->> 'head_sha'
                AND (h.pr_number = item ->> 'number' OR (item IS NULL
                    AND COALESCE(r.run ->> 'event', 'pull_request') <> 'push'
                    AND r.run ->> 'head_branch' NOT IN ('devel', 'main')))
        )
    GROUP BY r.repo, r.run ->> 'head_sha', item ->> 'number', r.run ->> 'head_branch'
), heads AS MATERIALIZED (
    SELECT h.*, EXISTS (
        SELECT 1 FROM pr_events e WHERE e.repo = h.repo AND e.pr_number = h.pr_number
            AND e.at = h.at AND e.sha <> h.sha
    ) AS ambiguous FROM pr_heads h WHERE at BETWEEN p_from AND p_to
    UNION ALL
    SELECT *, false FROM fallback_heads WHERE at BETWEEN p_from AND p_to
), jobs AS MATERIALIZED (
    SELECT repo, github -> 'workflow_job' AS job, event_at, predicate
    FROM lake WHERE github -> 'workflow_job' IS NOT NULL
), workflow_contexts AS (
    SELECT DISTINCT repo, job ->> 'workflow_name' AS workflow, job ->> 'name' AS name
    FROM jobs JOIN required ON required.name = job ->> 'name'
), check_events AS MATERIALIZED (
    SELECT DISTINCT j.repo, job ->> 'head_sha' AS sha, job ->> 'name' AS name,
        (job ->> 'run_id')::bigint AS run_id,
        COALESCE((job ->> 'run_attempt')::integer, 1) AS attempt,
        CASE WHEN predicate = 'finished' THEN (job ->> 'completed_at')::timestamptz
            ELSE (job ->> 'started_at')::timestamptz END AS at,
        CASE WHEN predicate <> 'finished' THEN 'pending'
            WHEN job ->> 'conclusion' IN ('success', 'skipped', 'neutral') THEN 'green'
            WHEN job ->> 'conclusion' IN ('failure', 'timed_out', 'action_required', 'startup_failure') THEN 'red'
            WHEN job ->> 'conclusion' IN ('cancelled', 'stale') THEN 'cancelled'
            ELSE 'unknown' END AS status,
        CASE WHEN predicate = 'finished' THEN 2 ELSE 1 END AS priority
    FROM jobs j JOIN required ON required.name = job ->> 'name'
    UNION
    -- A queued rerun invalidates an older result before its job starts. Use
    -- webhook transition clocks only: REST created_at is the ORIGINAL run clock.
    SELECT r.repo, run ->> 'head_sha', c.name, (run ->> 'id')::bigint,
        COALESCE((run ->> 'run_attempt')::integer, 1), event_at,
        CASE WHEN run ->> 'conclusion' = 'cancelled' THEN 'cancelled' ELSE 'pending' END, 0
    FROM runs r JOIN workflow_contexts c ON c.repo = r.repo AND c.workflow = run ->> 'name'
    WHERE github ->> 'action' IN ('requested', 'in_progress')
        OR (github ->> 'action' = 'completed' AND run ->> 'conclusion' = 'cancelled'
            AND NOT EXISTS (
                SELECT 1 FROM jobs j WHERE j.repo = r.repo
                    AND j.job ->> 'run_id' = run ->> 'id' AND j.job ->> 'name' = c.name
                    AND COALESCE(j.job ->> 'run_attempt', '1') = COALESCE(run ->> 'run_attempt', '1')
                    AND j.predicate = 'finished' AND j.event_at <= r.event_at
            ))
), samples AS MATERIALIZED (
    SELECT h.*, point.at AS point_at, verdict.*
    FROM heads h
    CROSS JOIN LATERAL (
        SELECT DISTINCT e.at FROM check_events e
        WHERE e.repo = h.repo AND e.sha = h.sha AND e.at >= h.at
            AND e.at <= LEAST(COALESCE(h.replaced_at, p_to), p_to)
        UNION SELECT LEAST(COALESCE(h.replaced_at, p_to), p_to)
    ) point
    CROSS JOIN LATERAL (
        SELECT count(s.status)::integer AS observed,
            count(*) FILTER (WHERE s.status = 'green')::integer AS greens,
            count(*) FILTER (WHERE s.status = 'red')::integer AS reds,
            count(*) FILTER (WHERE s.status = 'cancelled')::integer AS cancels,
            count(*) FILTER (WHERE s.status = 'unknown')::integer AS unknowns,
            string_agg(required.name, ', ' ORDER BY required.name)
                FILTER (WHERE s.status IS NULL OR s.status NOT IN ('green', 'red', 'cancelled')) AS missing
        FROM required
        LEFT JOIN LATERAL (
            SELECT e.status FROM check_events e
            WHERE e.repo = h.repo AND e.sha = h.sha AND e.name = required.name
                AND e.at >= h.at AND e.at <= point.at
            ORDER BY e.run_id DESC, e.attempt DESC, e.at DESC, e.priority DESC
            LIMIT 1
        ) s ON true
    ) verdict
), history AS (
    SELECT repo, sha, pr_number, head_branch, at, start_clock, replaced_at, ambiguous,
        min(point_at) FILTER (WHERE reds > 0 AND NOT ambiguous
            AND COALESCE((SELECT (data ->> 'supported')::boolean FROM policy), false)) AS red_at,
        min(point_at) FILTER (WHERE greens = (SELECT count(*) FROM required)
            AND greens > 0 AND NOT ambiguous
            AND COALESCE((SELECT (data ->> 'supported')::boolean FROM policy), false)) AS green_at
    FROM samples GROUP BY repo, sha, pr_number, head_branch, at, start_clock, replaced_at, ambiguous
), latest AS (
    SELECT DISTINCT ON (repo, sha, pr_number, at) * FROM samples
    ORDER BY repo, sha, pr_number, at, point_at DESC
)
SELECT h.repo, h.sha, h.pr_number, h.head_branch, h.at, h.start_clock, h.replaced_at,
    h.red_at, h.green_at,
    CASE WHEN h.red_at <= COALESCE(h.green_at, h.red_at) THEN 'red'
        WHEN h.green_at IS NOT NULL THEN 'green' END,
    extract(epoch FROM (LEAST(h.red_at, h.green_at) - h.at)),
    extract(epoch FROM (h.red_at - h.at)), extract(epoch FROM (h.green_at - h.at)),
    CASE WHEN h.ambiguous OR NOT COALESCE((SELECT (data ->> 'supported')::boolean FROM policy), false)
            OR (SELECT count(*) FROM required) = 0 THEN 'unknown'
        WHEN l.reds > 0 THEN 'red'
        WHEN l.greens = (SELECT count(*) FROM required) THEN 'green'
        WHEN h.replaced_at <= p_to THEN 'superseded'
        WHEN l.unknowns > 0 THEN 'unknown'
        WHEN l.cancels > 0 THEN 'cancelled' ELSE 'pending' END,
    l.observed, (SELECT count(*)::integer FROM required),
    CASE WHEN h.ambiguous THEN 'ambiguous PR head order' ELSE l.missing END,
    (SELECT observed_at FROM policy),
    'https://github.com/' || h.repo || '/commit/' || h.sha
FROM history h JOIN latest l
    ON l.repo = h.repo AND l.sha = h.sha AND l.pr_number IS NOT DISTINCT FROM h.pr_number AND l.at = h.at;
$$;
