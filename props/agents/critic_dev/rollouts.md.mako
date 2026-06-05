# Analyzing Agent Rollouts

Diagnostic queries for agent execution traces. Schema and basic queries are in the LLM Requests and Cost Tracking docs — this covers higher-level analysis patterns.

## Extracting Tool Calls

Tool-call JSON depends on `llm_requests.api_shape`.

### Responses calls

```sql
SELECT r.created_at, r.model,
       tc->>'name' AS tool_name, tc->'arguments' AS args
FROM llm_requests r,
    jsonb_array_elements(r.response_body->'output') AS tc
WHERE r.agent_run_id = '<run_id>'
  AND r.api_shape = 'responses'
  AND tc->>'type' = 'function_call'
ORDER BY r.created_at;
```

### Responses calls with outputs

```sql
WITH calls AS (
    SELECT r.created_at, r.model,
           tc->>'id' AS call_id, tc->>'name' AS tool_name,
           tc->'arguments' AS args
    FROM llm_requests r,
        jsonb_array_elements(r.response_body->'output') AS tc
    WHERE r.agent_run_id = '<run_id>'
      AND r.api_shape = 'responses'
      AND tc->>'type' = 'function_call'
),
outputs AS (
    SELECT item->>'call_id' AS call_id, item->>'output' AS output
    FROM llm_requests r,
        jsonb_array_elements(r.request_body->'input') AS item
    WHERE r.agent_run_id = '<run_id>'
      AND r.api_shape = 'responses'
      AND item->>'type' = 'function_call_output'
)
SELECT c.created_at, c.model, c.tool_name, c.args,
       LEFT(o.output, 500) AS output_preview
FROM calls c
LEFT JOIN outputs o ON c.call_id = o.call_id
ORDER BY c.created_at;
```

### Chat Completions calls

```sql
SELECT r.created_at, r.model,
       tc->'function'->>'name' AS tool_name,
       tc->'function'->>'arguments' AS args
FROM llm_requests r,
     jsonb_array_elements(r.response_body->'choices') AS choice,
     jsonb_array_elements(COALESCE(choice->'message'->'tool_calls', '[]'::jsonb)) AS tc
WHERE r.agent_run_id = '<run_id>'
  AND r.api_shape = 'chat_completions'
ORDER BY r.created_at;
```

### Chat Completions tool outputs

```sql
SELECT r.created_at, msg->>'tool_call_id' AS call_id,
       LEFT(msg->>'content', 500) AS output_preview
FROM llm_requests r,
     jsonb_array_elements(r.request_body->'messages') AS msg
WHERE r.agent_run_id = '<run_id>'
  AND r.api_shape = 'chat_completions'
  AND msg->>'role' = 'tool'
ORDER BY r.created_at;
```

## Diagnostic Patterns

### Find timed-out runs

```sql
SELECT ar.agent_run_id, ar.model,
       ar.type_config->'example'->>'snapshot_slug' AS snapshot
FROM agent_runs ar
WHERE ar.status = 'timed_out'
  AND ar.type_config->>'agent_type' = 'critic';
```

### Find runs that exceeded budget

```sql
SELECT ar.agent_run_id, ar.budget_usd,
       ar.type_config->'example'->>'snapshot_slug' AS snapshot,
       SUM(lrc.cost_usd) AS total_cost
FROM agent_runs ar
JOIN llm_run_costs lrc ON ar.agent_run_id = lrc.agent_run_id
WHERE ar.type_config->>'agent_type' = 'critic'
  AND ar.budget_usd IS NOT NULL
GROUP BY ar.agent_run_id
HAVING SUM(lrc.cost_usd) >= ar.budget_usd;
```

### Count LLM requests per run

```sql
SELECT
    ar.agent_run_id,
    ar.type_config->'example'->>'snapshot_slug' AS snapshot,
    COUNT(*) AS n_requests,
    SUM(r.input_tokens) AS total_input_tokens,
    SUM(r.output_tokens) AS total_output_tokens
FROM agent_runs ar
JOIN llm_requests r ON ar.agent_run_id = r.agent_run_id
WHERE ar.type_config->>'agent_type' = 'critic'
GROUP BY ar.agent_run_id
ORDER BY n_requests DESC;
```

## RLS Notes

- **TRAIN split:** Full access to LLM requests for all critic/grader runs
- **VALID/TEST splits:** LLM requests are RLS-blocked to prevent overfitting

Query only TRAIN split runs when analyzing execution traces.
