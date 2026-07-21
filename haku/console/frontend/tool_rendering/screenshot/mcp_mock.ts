export type OperatorMcpToolFixture = (args: Record<string, unknown>) => unknown;
export type OperatorMcpToolFixtures = Readonly<Record<string, OperatorMcpToolFixture>>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function callToolResult(value: unknown): Record<string, unknown> {
  const wrap = typeof value !== "object" || value === null || Array.isArray(value);
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    structuredContent: wrap ? { result: value } : value,
    isError: false,
    ...(wrap ? { _meta: { fastmcp: { wrap_result: true } } } : {}),
  };
}

async function requestJson(input: RequestInfo | URL, init?: RequestInit): Promise<Record<string, unknown>> {
  const body = init?.body ?? (input instanceof Request ? await input.clone().text() : null);
  if (typeof body !== "string") return {};
  return JSON.parse(body) as Record<string, unknown>;
}

function toolPayload(name: string, args: Record<string, unknown>, fixtures: OperatorMcpToolFixtures): unknown {
  const fixture = fixtures[name];
  if (fixture !== undefined) return fixture(args);
  throw new Error(`Screenshot MCP mock has no configured response for ${name}`);
}

export function installOperatorMcpMock(fixtures: OperatorMcpToolFixtures): void {
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const response = await mockOperatorMcpFetch(input, init, url, fixtures);
    if (response !== null) return response;
    if (realFetch) return realFetch(input, init);
    return jsonResponse({});
  }) as typeof fetch;
}

/** Return a canned response for the operator MCP bootstrap/call sequence, or null for non-MCP URLs. */
export async function mockOperatorMcpFetch(
  input: RequestInfo | URL,
  init: RequestInit | undefined,
  url: string,
  fixtures: OperatorMcpToolFixtures
): Promise<Response | null> {
  if (!url.includes("/mcp")) return null;
  const method = init?.method ?? (input instanceof Request ? input.method : "GET");
  if (method === "GET") return new Response(null, { status: 405 });
  const request = await requestJson(input, init);
  if (request.method === "notifications/initialized") return new Response(null, { status: 202 });
  if (request.method === "initialize") {
    const params = request.params as { protocolVersion?: unknown } | undefined;
    return jsonResponse({
      jsonrpc: "2.0",
      id: request.id,
      result: {
        protocolVersion: params?.protocolVersion ?? "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "haku-console-screenshot", version: "1" },
      },
    });
  }
  if (request.method === "tools/call") {
    const params = request.params as { name: string; arguments?: Record<string, unknown> };
    return jsonResponse({
      jsonrpc: "2.0",
      id: request.id,
      result: callToolResult(toolPayload(params.name, params.arguments ?? {}, fixtures)),
    });
  }
  return jsonResponse({ jsonrpc: "2.0", id: request.id, error: { code: -32601, message: "not mocked" } });
}
