# LLM Requests and Costs

## LLM Requests Table

The `llm_requests` table stores LLM API request/response payloads logged by the proxy for debugging and analysis.

${describe_relation("llm_requests")}

Each row captures one LLM proxy call made by an agent. Use `api_shape` to read
the raw JSON correctly:

- `responses`: OpenAI Responses API payloads (`request_body->'input'`,
  `response_body->'output'`)
- `chat_completions`: OpenAI Chat Completions payloads
  (`request_body->'messages'`, `response_body->'choices'`)

### Queries

All LLM requests for a specific agent run:

```sql
SELECT model, api_shape, latency_ms, error, created_at
FROM llm_requests
WHERE agent_run_id = '<uuid>'
ORDER BY created_at;
```

Full request/response payloads for debugging:

```sql
SELECT api_shape, request_body, response_body, error
FROM llm_requests
WHERE agent_run_id = '<uuid>'
ORDER BY created_at;
```

Failed requests:

```sql
SELECT * FROM llm_requests
WHERE agent_run_id = '<uuid>' AND error IS NOT NULL;
```

## Cost Tracking

### Cost Formula

LLM request cost is computed as:

```
cost_usd = (input_tokens - cached_tokens) * input_rate
         + cached_tokens * cached_rate
         + output_tokens * output_rate
```

Where rates are per-token prices from `model_metadata` (USD per 1M tokens, divided by 1M).

### Views

${describe_relation("llm_request_costs")}
${describe_relation("llm_run_costs")}

### Queries

Cost of a specific run (including children), per model:

```sql
SELECT * FROM llm_run_costs WHERE agent_run_id = '<uuid>';
```

Total cost of a run across all models:

```sql
SELECT agent_run_id, SUM(cost_usd) AS total_cost
FROM llm_run_costs WHERE agent_run_id = '<uuid>'
GROUP BY agent_run_id;
```

Per-request breakdown for a run:

```sql
SELECT * FROM llm_request_costs WHERE agent_run_id = '<uuid>' ORDER BY created_at;
```
