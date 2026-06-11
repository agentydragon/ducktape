---
name: debug_agent_traces
description: Debug props agent traces. Read LLM request/response history, parse tool calls, and "speak with dead" — resurrect a past agent conversation to ask follow-up questions about its decisions.
argument-hint: "<agent_run_id or question>"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, WebFetch, Task
---

# Debug Agent Traces

Debug props agent behavior by reading their LLM traces from the database and
optionally resurrecting past conversations to ask follow-up questions.

**Argument:** `$ARGUMENTS`

## Prerequisites

- PostgreSQL running at `127.0.0.1:5433`
- `OPENAI_API_KEY` in environment (for speak-with-dead)

## Database Connection

```bash
export PGHOST=127.0.0.1
export PGPORT=5433
export PGUSER=postgres
export PGPASSWORD=$(cat props/.devenv/state/pg_password)
export PGDATABASE=eval_results
```

## Capabilities

### 1. List Agent Runs

Find runs to debug:

```sql
SELECT agent_run_id, status,
       type_config->>'agent_type' as agent_type,
       type_config->'example'->>'snapshot_slug' as snapshot,
       created_at
FROM agent_runs
ORDER BY created_at DESC;
```

### 2. Read LLM Trace

For a given `agent_run_id`, list all LLM round-trips:

```sql
SELECT id, model, input_tokens, output_tokens, latency_ms, error, created_at
FROM llm_requests
WHERE agent_run_id = '<run_id>'
ORDER BY created_at;
```

### 3. Parse Tool Calls from a Trace

LLM requests/responses use **OpenAI Responses API** format:

- `request_body` has keys: `input` (conversation array), `model`, `tools`,
  `instructions` (sometimes null — check `input[0]` for system message)
- `response_body` has key: `output` (array of response items)

**Input items** have `role`:

- `system` — system prompt (check `content[0].text`)
- `function_call` — tool call from model (has `name`, `arguments`, `call_id`)
- `function_call_output` — tool result (has `call_id`, `output`)

**Output items** have `type`:

- `message` — text response (`content[0].text`)
- `function_call` — tool call (`name`, `arguments`, `call_id`)
- `reasoning` — reasoning trace if model supports it (`summary`)

To extract all tool calls from a run, save each `response_body` and parse:

```python
import json

# For each response_body:
output = response_body.get('output', [])
for item in output:
    if item['type'] == 'function_call':
        name = item['name']
        args = json.loads(item['arguments'])
        print(f'{name}({args})')
    elif item['type'] == 'message':
        text = item['content'][0]['text']
        print(f'TEXT: {text}')
    elif item['type'] == 'reasoning':
        summaries = item.get('summary', [])
        print(f'REASONING: {summaries}')
```

### 4. Read Specific Turn

To read the full request and response for a specific LLM turn:

```sql
SELECT request_body::text, response_body::text
FROM llm_requests WHERE id = <turn_id>;
```

Save to files and parse with Python to inspect the full conversation context,
tools available, and model's response.

### 5. Speak With Dead (Resurrect Conversation)

Resurrect a past agent conversation to ask follow-up questions. This sends the
agent's original conversation prefix (up to a chosen turn) plus a new question
to the same model, getting back the agent's perspective.

**Steps:**

1. **Pick the turn** — find the `llm_requests.id` at or after the decision
   you want to ask about.

2. **Extract the conversation prefix** — the `request_body.input` array
   contains all turns up to that point. The `response_body.output` array
   contains the model's response for that turn.

3. **Build the resurrection request:**

   ```python
   import json
   import httpx
   import os

   # Load the turn
   # request_body and response_body from the database for the chosen turn

   # Take the original request as base
   resurrection = {
       "model": request_body["model"],
       "input": request_body["input"].copy(),
       "tools": request_body["tools"],
       "instructions": request_body.get("instructions"),
   }

   # Append the model's response from that turn
   for item in response_body["output"]:
       resurrection["input"].append(item)

   # If there are subsequent turns to include, append their
   # function_call_output and further exchanges from later request_bodies.
   # Each subsequent request_body.input will have new items appended after
   # the previous response — extract just the NEW items (tool results, etc.)

   # Add the follow-up question
   resurrection["input"].append({
       "role": "user",
       "content": "Why did you assign credit=1.0 to all TPs for the "
                  "broad-except issue? What was your reasoning?"
   })

   # Send to OpenAI
   resp = httpx.post(
       "https://api.openai.com/v1/responses",
       headers={
           "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
           "Content-Type": "application/json",
       },
       json=resurrection,
       timeout=60,
   )
   result = resp.json()

   # Extract answer
   for item in result.get("output", []):
       if item.get("type") == "message":
           for c in item.get("content", []):
               if c.get("type") == "output_text":
                   print(c["text"])
   ```

4. **Include full context** — for best results, include all turns up to and
   including the turn of interest. The model needs the same context it had
   when making the decision.

**Shortcut: include ALL turns up to turn N:**

Rather than manually stitching, you can reconstruct the full conversation from
sequential `llm_requests` rows. Each `request_body.input` for turn N+1
contains all of turn N's prefix + turn N's response + any new tool results.
So the **last request_body before or at your target turn** already has the
full prefix. Just append that turn's response and your question.

```python
# To ask about turn 58's decision:
# 1. Load request_body from turn 58 (has full prefix)
# 2. Load response_body from turn 58 (has the model's decision)
# 3. Append response output items to input
# 4. Append your question
# 5. Send to API
```

### 6. View Grading Results

Check what a grader decided:

```sql
-- Grading edges for a specific critic run
SELECT ge.critique_issue_id,
       COALESCE(ge.tp_id, ge.fp_id) as ground_truth_id,
       CASE WHEN ge.tp_id IS NOT NULL THEN 'TP' ELSE 'FP' END as edge_type,
       ge.credit,
       LEFT(ge.rationale, 120) as rationale
FROM grading_edges ge
WHERE ge.critique_run_id = '<critic_run_id>'
ORDER BY ge.critique_issue_id, edge_type, ground_truth_id;
```

Check grading completeness:

```sql
SELECT COUNT(*) as pending FROM grading_pending
WHERE critique_run_id = '<critic_run_id>';
-- 0 = complete, >0 = still grading
```

### 7. View Critic Findings

```sql
SELECT ri.issue_id, ri.rationale,
       rio.locations
FROM reported_issues ri
LEFT JOIN reported_issue_occurrences rio
  ON rio.agent_run_id = ri.agent_run_id
  AND rio.reported_issue_id = ri.issue_id
WHERE ri.agent_run_id = '<critic_run_id>';
```

### 8. Cost Analysis

```sql
-- Per-run costs
SELECT agent_run_id, model, total_cost, total_input_tokens, total_output_tokens
FROM llm_run_costs;

-- Per-request breakdown
SELECT id, model, input_tokens, cached_input_tokens, output_tokens, latency_ms
FROM llm_requests
WHERE agent_run_id = '<run_id>'
ORDER BY created_at;
```

## Tips

- **Turn IDs are sequential** — lower `llm_requests.id` = earlier in the
  conversation. Use `ORDER BY created_at` to see chronological order.
- **The last request has the longest input** — each turn appends to the
  conversation, so `input_tokens` grows monotonically.
- **Reasoning models** (o-series, etc.) will have `reasoning` items in output
  with `summary` arrays showing chain-of-thought.
- **gpt-4.1-mini** is the default grader model — it doesn't produce reasoning
  traces but is fast and cheap.
- **For speak-with-dead**, using the same model preserves behavior fidelity.
  Using a stronger model may give better explanations but different reasoning.
