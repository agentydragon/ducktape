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
