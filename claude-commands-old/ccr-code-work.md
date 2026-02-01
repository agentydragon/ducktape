# Work with Claude Code Router

We will work with my Claude Code Router setup.

Research the following files and directories and get familiar with the wiring:

@~/.claude-code-router
@~/.claude-code-router/plugins/system-rewriter.js
@~/.claude-code-router/config.json
@~/code/claude-code-router
@~/code/claude-code-router/src/server.ts

Use github MCP server when it's useful to reach into the underlying server the claude-code-router is implemented on.

Particularly, make sure you understand:

- How system-rewriter works
- How logging from claude-code-router works, particularly wire logging
- Make sure you understand the proxy flow and how we capture flows in logs

Make sure you thoroughly understand how this works to proxy Claude Code calls with system message rewriting.
Read surrounding documentation.

Once you are very confident you thoroughly understand all implemented functions, briefly report back what this stack does and await further instructions.

---

## Dependency: @musistudio/llms (GitHub MCP quick fetches)

Use the GitHub MCP tools to inspect the underlying server implementation used by the router.

Suggested sequence (copy/paste these MCP calls):

1. Locate the repo (optional)

```
mcp__github__search_code
  query: filename:package.json "name": "@musistudio/llms"
  perPage: 50
```

2. Read package metadata (confirm entry points/exports)

```
mcp__github__get_file_contents
  owner: musistudio
  repo: llms
  path: package.json
```

3. List repository root (see src/, scripts/)

```
mcp__github__get_file_contents
  owner: musistudio
  repo: llms
  path: "/"
```

4. List main sources directory

```
mcp__github__get_file_contents
  owner: musistudio
  repo: llms
  path: "/src/"
```

5. Read the server bootstrap (primary entry for Fastify server)

```
mcp__github__get_file_contents
  owner: musistudio
  repo: llms
  path: "src/server.ts"
```

6. (Optional) Drill into key subpaths

```
mcp__github__get_file_contents { owner: musistudio, repo: llms, path: "src/api" }
mcp__github__get_file_contents { owner: musistudio, repo: llms, path: "src/services" }
mcp__github__get_file_contents { owner: musistudio, repo: llms, path: "src/transformer" }
mcp__github__get_file_contents { owner: musistudio, repo: llms, path: "src/utils" }
```

Main paths to review in @musistudio/llms:

- src/server.ts
- src/api/
- src/services/
- src/transformer/
- src/types/
- src/utils/

---

## One-shot parallel batch (local + MCP reads in a single message)

Run all reads in parallel using the multi-tool parallel wrapper. Issue ONE message containing a single multi_tool_use.parallel call with a tool_uses array covering every local file read and GitHub fetch.

Example payload (adjust paths if your home differs):

- functions.Read (local files)
  - file_path: /Users/mpokorny/.claude-code-router/transformers/edit-error-annotator.js
  - file_path: /Users/mpokorny/.claude-code-router/transformers/lib/system-utils.js
  - file_path: /Users/mpokorny/.claude-code-router/transformers/openai-image-fix.js
  - file_path: /Users/mpokorny/.claude-code-router/transformers/openai-reasoning.js
  - file_path: /Users/mpokorny/.claude-code-router/transformers/system-replace.js
  - file_path: /Users/mpokorny/code/claude-code-router/src/middleware/tracing.ts
  - file_path: /Users/mpokorny/code/claude-code-router/src/tracing/context.ts
  - file_path: /Users/mpokorny/code/claude-code-router/src/tracing/interceptor.ts
  - file_path: /Users/mpokorny/code/claude-code-router/src/tracing/sanitize.ts
  - file_path: /Users/mpokorny/code/claude-code-router/src/utils/log.ts
  - file_path: /Users/mpokorny/code/claude-code-router/src/utils/tracer.ts

- `functions.mcp__github__get_file_contents` (musistudio/llms)
  - { owner: musistudio, repo: llms, path: "package.json" }
  - { owner: musistudio, repo: llms, path: "/" }
  - { owner: musistudio, repo: llms, path: "/src/" }
  - { owner: musistudio, repo: llms, path: "src/server.ts" }
  - { owner: musistudio, repo: llms, path: "src/api" }
  - { owner: musistudio, repo: llms, path: "src/services" }
  - { owner: musistudio, repo: llms, path: "src/transformer" }
  - { owner: musistudio, repo: llms, path: "src/utils" }

- `functions.mcp__github__search_code` (OpenAI Cookbook: reasoning model docs)
  - { query: "repo:openai/openai-cookbook (o3 OR reasoning OR \"max_completion_tokens\" OR \"reasoning models\")", perPage: 50 }

- `functions.mcp__github__get_file_contents` (OpenAI Cookbook repo top-level directories for quick skimming)
  - { owner: openai, repo: openai-cookbook, path: "/" }
  - { owner: openai, repo: openai-cookbook, path: "guides" }
  - { owner: openai, repo: openai-cookbook, path: "examples" }
  - { owner: openai, repo: openai-cookbook, path: "apps" }

- `functions.mcp__github__get_file_contents` (specific cookbook examples)
  - { owner: openai, repo: openai-cookbook, path: "examples/responses_api/reasoning_items.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/responses_api/responses_example.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/responses_api/responses_api_tool_orchestration.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/reasoning_function_calls.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/o-series/o3o4-mini_prompting_guide.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/gpt-5/gpt-5_new_params_and_tools.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/Prompt_migration_guide.ipynb" }
  - { owner: openai, repo: openai-cookbook, path: "examples/File_Search_Responses.ipynb" }

- `functions.mcp__github__get_file_contents` (OpenAI Python SDK type definitions)
  - { owner: openai, repo: openai-python, path: "src/openai/types/responses/response_create_params.py" }
  - { owner: openai, repo: openai-python, path: "src/openai/types/shared_params/reasoning.py" }
  - { owner: openai, repo: openai-python, path: "src/openai/types/chat/completion_create_params.py" }
  - { owner: openai, repo: openai-python, path: "src/openai/types/chat/chat_completion_stream_options_param.py" }

Notes:

- Prefer a single multi_tool_use.parallel call with all of the above entries in tool_uses so they execute concurrently.
- After search_code returns, optionally issue a second parallel batch to fetch additional cookbook/example files surfaced by the search.
- Keep outputs concise. Sensitive headers are redacted by the tracer when applicable; no secrets should appear in fetched sources.

---

## OpenAI Reasoning Models — authoritative pointers

Docs (canonical):

- Models — <https://platform.openai.com/docs/models>
- Models (Reasoning section) — <https://platform.openai.com/docs/models#reasoning>
- Model card: o3 — <https://platform.openai.com/docs/models/o3>
- Model card: o3-mini — <https://platform.openai.com/docs/models/o3-mini>
- Responses API (overview) — <https://platform.openai.com/docs/api-reference/responses>
- Responses API (Create, request body) — <https://platform.openai.com/docs/api-reference/responses/create#request-body>
- Responses API (Streaming) — <https://platform.openai.com/docs/api-reference/responses/streaming>
- Chat Completions (overview) — <https://platform.openai.com/docs/api-reference/chat>
- Chat Completions (Create, request body) — <https://platform.openai.com/docs/api-reference/chat/completions#request-body>
- Assistants overview — <https://platform.openai.com/docs/assistants/overview#assistants-api>
- Assistants how it works — <https://platform.openai.com/docs/assistants/how-it-works#how-it-works>

API parameter summary (reasoning models):

- Responses API (/v1/responses)
  - Tokens: max_output_tokens (bounds include reasoning + visible output tokens)
  - Reasoning: reasoning { effort: minimal|low|medium|high; summary: auto|concise|detailed } (generate_summary deprecated)
  - Sampling: temperature, top_p, top_logprobs
  - Streaming: stream: true; stream_options.include_obfuscation (SSE)
  - Tools: tools, tool_choice, parallel_tool_calls, max_tool_calls
  - Conversation/state: conversation | previous_response_id; include, truncation, store
  - Routing & safety: service_tier, prompt_cache_key, safety_identifier
- Chat Completions API (/v1/chat/completions)
  - Tokens: max_completion_tokens (preferred); max_tokens is deprecated and not compatible with o-series
  - Reasoning: reasoning_effort (minimal|low|medium|high)
  - Sampling: temperature, top_p
  - Streaming: stream: true; stream_options.include_obfuscation, stream_options.include_usage
  - Tools: tools, tool_choice, parallel_tool_calls
  - Caveat: stop not supported with latest reasoning models (o3, o4-mini)

Cookbook (retrieve via MCP; repo paths):

- examples/responses_api/reasoning_items.ipynb
- examples/responses_api/responses_example.ipynb
- examples/responses_api/responses_api_tool_orchestration.ipynb
- examples/reasoning_function_calls.ipynb
- examples/o-series/o3o4-mini_prompting_guide.ipynb
- examples/gpt-5/gpt-5_new_params_and_tools.ipynb
- examples/Prompt_migration_guide.ipynb
- examples/File_Search_Responses.ipynb

SDK Types (retrieve via MCP; repo paths):

- openai/openai-python — src/openai/types/responses/response_create_params.py
- openai/openai-python — src/openai/types/shared_params/reasoning.py
- openai/openai-python — src/openai/types/chat/completion_create_params.py
- openai/openai-python — src/openai/types/chat/chat_completion_stream_options_param.py

Mapping guidance (field translations):

- Tokens
  - Responses: max_output_tokens (correct). Remove max_tokens / max_completion_tokens.
  - Chat Completions: max_completion_tokens (correct). Remove max_tokens (deprecated; incompatible with o-series).
- Reasoning controls
  - Responses: reasoning { effort: minimal|low|medium|high; summary: auto|concise|detailed }.
  - Chat Completions: reasoning_effort (minimal|low|medium|high). No summary field.
- Streaming
  - Responses: stream: true; stream_options.include_obfuscation.
  - Chat Completions: stream: true; stream_options.include_obfuscation, stream_options.include_usage.
- Stop sequences
  - Chat Completions: stop not supported for o3/o4-mini → omit.
  - Responses: no "stop"; use token bounds/truncation.
- Temperature/sampling
  - Both APIs: temperature/top_p supported (model behavior may differ).
