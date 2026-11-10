/**
 * Fixtures for the LLM-request viewer: OpenAI Responses API turns — a text answer, a tool-call
 * round trip, and a failed request.
 */
export const llmRequests = [
  {
    // Multi-turn conversation: instructions + user message; text response with cached tokens
    id: 1,
    model: "gpt-4o",
    request_body: {
      model: "gpt-4o",
      instructions:
        "You are a security code reviewer. Analyze code carefully and report any security vulnerabilities you find.",
      input: [
        {
          role: "user",
          content: [
            {
              type: "input_text",
              text: "Review this Python code for security issues:\n\n```python\nimport hashlib\nimport os\n\ndef hash_password(password: str) -> str:\n    return hashlib.md5(password.encode()).hexdigest()\n\ndef verify_user(username: str, password: str) -> bool:\n    stored_hash = get_stored_hash(username)\n    return stored_hash == hash_password(password)\n```",
            },
          ],
        },
      ],
      max_output_tokens: 4096,
    },
    response_body: {
      id: "resp_abc123",
      object: "response",
      model: "gpt-4o-2024-08-06",
      status: "completed",
      output: [
        {
          type: "message",
          id: "msg_abc123",
          role: "assistant",
          status: "completed",
          content: [
            {
              type: "output_text",
              text: "I found several security issues:\n\n1. **Weak hash algorithm**: MD5 is cryptographically broken and should not be used for password hashing. Use bcrypt, scrypt, or Argon2 instead.\n\n2. **No salt**: The hash is computed without a salt, making it vulnerable to rainbow table attacks.\n\n3. **Timing attack**: The string comparison `==` is not constant-time, enabling timing-based attacks to infer password hashes.",
            },
          ],
        },
      ],
      usage: {
        input_tokens: 250,
        output_tokens: 180,
        total_tokens: 430,
        input_tokens_details: { cached_tokens: 50 },
        output_tokens_details: { reasoning_tokens: 0 },
      },
    },
    error: null,
    latency_ms: 2341,
    created_at: "2025-01-20T10:00:00Z",
  },
  {
    // Multi-turn with tool calls: input includes previous assistant turn + function call + result
    id: 2,
    model: "gpt-4o",
    request_body: {
      model: "gpt-4o",
      instructions: "You are a security code reviewer. Use the report_issue tool to record each finding.",
      input: [
        {
          role: "user",
          content: [{ type: "input_text", text: "Review this code for security issues." }],
        },
        {
          type: "message",
          role: "assistant",
          id: "msg_prev",
          content: [{ type: "output_text", text: "I'll analyze the code and report any security issues I find." }],
        },
        {
          type: "function_call",
          id: "fc_001",
          call_id: "call_001",
          name: "report_issue",
          arguments: JSON.stringify({
            severity: "high",
            title: "MD5 password hashing",
            description: "MD5 is cryptographically broken.",
          }),
          status: "completed",
        },
        {
          type: "function_call_output",
          call_id: "call_001",
          output: JSON.stringify({ recorded: true, issue_id: "issue-001" }),
        },
      ],
      tools: [
        {
          type: "function",
          name: "report_issue",
          description: "Record a security issue found in the code under review.",
          parameters: {
            type: "object",
            properties: {
              severity: { type: "string", enum: ["low", "medium", "high", "critical"] },
              title: { type: "string" },
              description: { type: "string" },
            },
            required: ["severity", "title", "description"],
          },
        },
      ],
      tool_choice: "auto",
      max_output_tokens: 4096,
    },
    response_body: {
      id: "resp_def456",
      object: "response",
      model: "gpt-4o-2024-08-06",
      status: "completed",
      output: [
        {
          type: "function_call",
          id: "fc_002",
          call_id: "call_002",
          name: "report_issue",
          arguments: JSON.stringify({
            severity: "medium",
            title: "No rate limiting",
            description: "verify_user lacks rate limiting, enabling brute-force attacks.",
          }),
          status: "completed",
        },
        {
          type: "message",
          id: "msg_def456",
          role: "assistant",
          status: "completed",
          content: [
            {
              type: "output_text",
              text: "I've reported the missing rate limiting issue. The lack of request throttling on login attempts allows attackers to try unlimited passwords.",
            },
          ],
        },
      ],
      usage: {
        input_tokens: 350,
        output_tokens: 120,
        total_tokens: 470,
        input_tokens_details: { cached_tokens: 0 },
        output_tokens_details: { reasoning_tokens: 0 },
      },
    },
    error: null,
    latency_ms: 1567,
    created_at: "2025-01-20T10:01:00Z",
  },
  {
    // Failed request: no response body, error from upstream
    id: 3,
    model: "gpt-4o-mini",
    request_body: {
      model: "gpt-4o-mini",
      input: [
        {
          role: "user",
          content: [{ type: "input_text", text: "Summarize all findings so far." }],
        },
      ],
    },
    response_body: null,
    error: "Rate limit exceeded - retry after 30 seconds",
    latency_ms: 234,
    created_at: "2025-01-20T10:02:00Z",
  },
];

/** Raw JSON viewer states for the Chat Completions API shape, including an upstream error body. */
export const llmChatRequests = [
  {
    id: 4,
    model: "gpt-4o-mini",
    api_shape: "chat_completions",
    request_body: {
      model: "gpt-4o-mini",
      messages: [
        { role: "system", content: "Answer clearly and briefly." },
        { role: "user", content: "What is the capital of France?" },
      ],
      temperature: 0.2,
    },
    response_body: {
      id: "chatcmpl_abc123",
      object: "chat.completion",
      model: "gpt-4o-mini-2025-01-20",
      choices: [{ index: 0, message: { role: "assistant", content: "Paris." }, finish_reason: "stop" }],
      usage: { prompt_tokens: 24, completion_tokens: 2, total_tokens: 26 },
    },
    response_error_body: null,
    error: null,
    latency_ms: 412,
    created_at: "2025-01-20T10:03:00Z",
  },
  {
    id: 5,
    model: "gpt-4o-mini",
    api_shape: "chat_completions",
    request_body: {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "Generate a very long response." }],
    },
    response_body: null,
    response_error_body: {
      error: {
        message: "Request exceeds the context window.",
        type: "invalid_request_error",
        param: null,
        code: "context_length_exceeded",
      },
    },
    error: "Request failed with status 400",
    latency_ms: 89,
    created_at: "2025-01-20T10:04:00Z",
  },
];

/** Raw JSON viewer state for an Anthropic Messages request with text and tool blocks. */
export const llmAnthropicRequests = [
  {
    id: 6,
    model: "claude-3-7-sonnet-20250219",
    api_shape: "anthropic",
    request_body: {
      model: "claude-3-7-sonnet-20250219",
      max_tokens: 1024,
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: "Review this code for security issues." }],
        },
        {
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: "toolu_01HfakeReportIssue",
              name: "report_issue",
              input: { title: "MD5 password hashing" },
            },
          ],
        },
        {
          role: "user",
          content: [
            {
              type: "tool_result",
              tool_use_id: "toolu_01HfakeReportIssue",
              content: "Issue recorded as issue-001.",
            },
          ],
        },
      ],
      tools: [
        {
          name: "report_issue",
          input_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            required: ["title"],
          },
        },
      ],
    },
    response_body: {
      id: "msg_01HfakeAnthropicResponse",
      type: "message",
      role: "assistant",
      model: "claude-3-7-sonnet-20250219",
      content: [
        {
          type: "text",
          text: "The code uses MD5 for password hashing, which is too fast and vulnerable to cracking.",
        },
        {
          type: "tool_use",
          id: "toolu_01HfakeRateLimit",
          name: "report_issue",
          input: { title: "Missing rate limiting", severity: "medium" },
        },
      ],
      stop_reason: "tool_use",
      stop_sequence: null,
      usage: {
        input_tokens: 284,
        output_tokens: 74,
        cache_creation_input_tokens: 128,
        cache_read_input_tokens: 192,
      },
    },
    response_error_body: null,
    error: null,
    latency_ms: 1260,
    created_at: "2025-01-20T10:05:00Z",
  },
];
