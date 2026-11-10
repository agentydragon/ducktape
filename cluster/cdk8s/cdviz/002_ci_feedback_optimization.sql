-- Keep the feedback semantics of 001 while reducing intermediate row widths and
-- evaluating each head only against its own events.
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
    -- Decode each GitHub payload once, and retain only fields used below.
    SELECT timestamp AS event_at, predicate, gh.action,
        gh.pull_request #>> '{head,sha}' AS pr_sha, gh.pull_request ->> 'number' AS pr_number,
        gh.pull_request #>> '{head,ref}' AS pr_branch,
        gh.pull_request ->> 'created_at' AS pr_created_at, gh.pull_request ->> 'updated_at' AS pr_updated_at,
        COALESCE(gh.repository ->> 'full_name',
            split_part(payload #>> '{subject,content,pipelineName}', '/', 1) || '/' ||
            split_part(payload #>> '{subject,content,pipelineName}', '/', 2)) AS repo,
        gh.workflow_run IS NOT NULL AS is_run, gh.workflow_job IS NOT NULL AS is_job,
        gh.workflow_run -> 'pull_requests' AS run_pull_requests,
        r.head_sha AS run_sha, r.head_branch AS run_branch, r.id AS run_id,
        r.run_attempt AS run_attempt, r.created_at AS run_created_at,
        r.event AS run_event, r.name AS run_name, r.conclusion AS run_conclusion,
        j.head_sha AS job_sha, j.name AS job_name, j.workflow_name AS job_workflow,
        j.run_id AS job_run_id, j.run_attempt AS job_attempt,
        j.completed_at AS job_completed_at, j.started_at AS job_started_at, j.conclusion AS job_conclusion
    FROM cdviz.cdevents_lake
    CROSS JOIN LATERAL jsonb_to_record(
        CASE WHEN jsonb_typeof(payload #> '{customData,github}') = 'object'
            THEN payload #> '{customData,github}' ELSE '{}'::jsonb END
    ) AS gh(action text, repository jsonb, pull_request jsonb, workflow_run jsonb, workflow_job jsonb)
    CROSS JOIN LATERAL jsonb_to_record(
        CASE WHEN jsonb_typeof(gh.workflow_run) = 'object' THEN gh.workflow_run ELSE '{}'::jsonb END
    ) AS r(head_sha text, head_branch text, id text, run_attempt text, created_at text,
        event text, name text, conclusion text)
    CROSS JOIN LATERAL jsonb_to_record(
        CASE WHEN jsonb_typeof(gh.workflow_job) = 'object' THEN gh.workflow_job ELSE '{}'::jsonb END
    ) AS j(head_sha text, name text, workflow_name text, run_id text, run_attempt text,
        completed_at text, started_at text, conclusion text)
    WHERE timestamp >= p_from - interval '1 day' AND timestamp <= p_to
), recent_policy AS MATERIALIZED (
    -- Most selections have a recent policy replay. Limit the first search so a
    -- chunk without any policy does not have to scan its entire history.
    SELECT payload #> '{customData,ci_policy}' AS data, timestamp AS observed_at
    FROM cdviz.cdevents_lake
    WHERE timestamp <= p_to AND timestamp > p_to - interval '1 hour'
        AND payload #> '{customData,ci_policy}' IS NOT NULL
    ORDER BY timestamp DESC LIMIT 1
), policy AS MATERIALIZED (
    -- Fall back to all older history if there is no recent replay: historical
    -- selections and interrupted polls still use the latest available policy.
    SELECT * FROM recent_policy
    UNION ALL
    (SELECT payload #> '{customData,ci_policy}' AS data, timestamp AS observed_at
        FROM cdviz.cdevents_lake
        WHERE timestamp <= p_to - interval '1 hour'
            AND payload #> '{customData,ci_policy}' IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM recent_policy)
        ORDER BY timestamp DESC LIMIT 1)
), required AS (
    SELECT DISTINCT jsonb_array_elements_text(data -> 'contexts') AS name FROM policy
), pr_events AS (
    SELECT DISTINCT repo, pr_sha AS sha,
        pr_number AS pr_number,
        pr_branch AS head_branch,
        CASE WHEN action = 'opened'
            THEN (pr_created_at)::timestamptz
            ELSE (pr_updated_at)::timestamptz END AS at
    FROM lake
    WHERE action IN ('opened', 'synchronize')
        AND pr_sha IS NOT NULL
), pr_changes AS (
    SELECT *, lag(sha) OVER (PARTITION BY repo, pr_number ORDER BY at, sha) AS previous_sha
    FROM pr_events
), pr_heads AS (
    SELECT repo, sha, pr_number, head_branch, at, 'PR head event' AS start_clock,
        lead(at) OVER (PARTITION BY repo, pr_number ORDER BY at, sha) AS replaced_at
    FROM pr_changes WHERE sha IS DISTINCT FROM previous_sha
), runs AS MATERIALIZED (
    SELECT repo, event_at, action, run_sha AS sha, run_branch AS head_branch,
        run_id AS id, run_attempt AS attempt, run_created_at AS created_at,
        run_event AS event, run_name AS name, run_conclusion AS conclusion,
        run_pull_requests AS pull_requests
    FROM lake WHERE is_run
), fallback_heads AS (
    SELECT r.repo, r.sha AS sha, item ->> 'number' AS pr_number,
        r.head_branch AS head_branch,
        min((r.created_at)::timestamptz) AS at,
        'Workflow creation proxy' AS start_clock, NULL::timestamptz AS replaced_at
    FROM runs r
    LEFT JOIN LATERAL jsonb_array_elements(r.pull_requests) item ON true
    WHERE r.created_at IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM pr_heads h WHERE h.repo = r.repo AND h.sha = r.sha
                AND (h.pr_number = item ->> 'number' OR (item IS NULL
                    AND COALESCE(r.event, 'pull_request') <> 'push'
                    AND r.head_branch NOT IN ('devel', 'main')))
        )
    GROUP BY r.repo, r.sha, item ->> 'number', r.head_branch
), heads AS MATERIALIZED (
    SELECT h.*, EXISTS (
        SELECT 1 FROM pr_events e WHERE e.repo = h.repo AND e.pr_number = h.pr_number
            AND e.at = h.at AND e.sha <> h.sha
    ) AS ambiguous FROM pr_heads h WHERE at BETWEEN p_from AND p_to
    UNION ALL
    SELECT *, false FROM fallback_heads WHERE at BETWEEN p_from AND p_to
), jobs AS MATERIALIZED (
    SELECT repo, event_at, predicate, job_sha AS sha, job_name AS name, job_workflow AS workflow,
        job_run_id AS run_id, job_attempt AS attempt, job_completed_at AS completed_at,
        job_started_at AS started_at, job_conclusion AS conclusion
    FROM lake WHERE is_job
), workflow_contexts AS (
    SELECT DISTINCT repo, workflow, jobs.name
    FROM jobs JOIN required ON required.name = jobs.name
), finished_jobs AS (
    SELECT repo, run_id, name, COALESCE(attempt, '1') AS attempt, min(event_at) AS first_finished_at
    FROM jobs WHERE predicate = 'finished'
    GROUP BY repo, run_id, name, COALESCE(attempt, '1')
), check_events AS MATERIALIZED (
    SELECT DISTINCT j.repo, j.sha, j.name, j.run_id::bigint AS run_id,
        COALESCE(j.attempt::integer, 1) AS attempt,
        CASE WHEN predicate = 'finished' THEN completed_at::timestamptz
            ELSE started_at::timestamptz END AS at,
        CASE WHEN predicate <> 'finished' THEN 'pending'
            WHEN conclusion IN ('success', 'skipped', 'neutral') THEN 'green'
            WHEN conclusion IN ('failure', 'timed_out', 'action_required', 'startup_failure') THEN 'red'
            WHEN conclusion IN ('cancelled', 'stale') THEN 'cancelled'
            ELSE 'unknown' END AS status,
        CASE WHEN predicate = 'finished' THEN 2 ELSE 1 END AS priority
    FROM jobs j JOIN required ON required.name = j.name
    UNION
    -- A queued rerun invalidates an older result before its job starts. Use
    -- webhook transition clocks only: REST created_at is the ORIGINAL run clock.
    SELECT r.repo, r.sha, c.name, r.id::bigint,
        COALESCE(r.attempt::integer, 1), event_at,
        CASE WHEN r.conclusion = 'cancelled' THEN 'cancelled' ELSE 'pending' END, 0
    FROM runs r JOIN workflow_contexts c ON c.repo = r.repo AND c.workflow = r.name
    WHERE r.action IN ('requested', 'in_progress')
    UNION
    -- Keep cancellation separate so PostgreSQL can use a hash anti-join instead
    -- of rescanning every job for each workflow transition.
    SELECT r.repo, r.sha, c.name, r.id::bigint,
        COALESCE(r.attempt::integer, 1), event_at, 'cancelled', 0
    FROM runs r JOIN workflow_contexts c ON c.repo = r.repo AND c.workflow = r.name
    LEFT JOIN finished_jobs j ON j.repo = r.repo AND j.run_id = r.id AND j.name = c.name
        AND j.attempt = COALESCE(r.attempt, '1') AND j.first_finished_at <= r.event_at
    WHERE r.action = 'completed' AND r.conclusion = 'cancelled' AND j.run_id IS NULL
), numbered_heads AS MATERIALIZED (
    SELECT row_number() OVER () AS head_id, h.* FROM heads h
), head_events AS MATERIALIZED (
    -- Restrict once by head and its lifetime before considering sample times.
    SELECT h.head_id, e.name, e.run_id, e.attempt, e.at, e.status, e.priority
    FROM numbered_heads h JOIN check_events e ON e.repo = h.repo AND e.sha = h.sha
        AND e.at >= h.at AND e.at <= LEAST(COALESCE(h.replaced_at, p_to), p_to)
), points AS (
    SELECT head_id, at AS point_at FROM head_events
    UNION
    SELECT head_id, LEAST(COALESCE(replaced_at, p_to), p_to) FROM numbered_heads
), point_status AS (
    -- Select the same run/attempt/event precedence as the former lateral lookup,
    -- but join on head and gate instead of rescanning all events for every point.
    SELECT DISTINCT ON (p.head_id, p.point_at, r.name)
        p.head_id, p.point_at, r.name, e.status
    FROM points p LEFT JOIN required r ON true
    LEFT JOIN head_events e ON e.head_id = p.head_id AND e.name = r.name AND e.at <= p.point_at
    ORDER BY p.head_id, p.point_at, r.name, e.run_id DESC, e.attempt DESC, e.at DESC, e.priority DESC
), samples AS MATERIALIZED (
    SELECT h.*, p.point_at, count(p.status)::integer AS observed,
        count(*) FILTER (WHERE p.status = 'green')::integer AS greens,
        count(*) FILTER (WHERE p.status = 'red')::integer AS reds,
        count(*) FILTER (WHERE p.status = 'cancelled')::integer AS cancels,
        count(*) FILTER (WHERE p.status = 'unknown')::integer AS unknowns,
        string_agg(p.name, ', ' ORDER BY p.name)
            FILTER (WHERE p.status IS NULL OR p.status NOT IN ('green', 'red', 'cancelled')) AS missing
    FROM numbered_heads h JOIN point_status p ON p.head_id = h.head_id
    GROUP BY h.head_id, h.repo, h.sha, h.pr_number, h.head_branch, h.at,
        h.start_clock, h.replaced_at, h.ambiguous, p.point_at
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
