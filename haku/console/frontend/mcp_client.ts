import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { fetchCsrfToken, refreshCsrfToken } from "./client.ts";
import { mcpToolError, unwrapMcpToolResult } from "./mcp_result.ts";

let connectedClient: Promise<Client> | null = null;

function redirectToLogin(): void {
  if (typeof window !== "undefined" && !window.location.pathname.startsWith("/auth/")) {
    window.location.assign("/auth/login");
  }
}

async function connectOperatorMcp(): Promise<Client> {
  const transport = new StreamableHTTPClientTransport(new URL("/mcp", document.baseURI), {
    requestInit: { credentials: "same-origin" },
    // The SDK invokes this for initialize, notifications, calls, and its optional SSE probe.
    // Resolve the current double-submit token each time so a legitimate console action that
    // refreshes the signed cookie cannot leave a long-lived transport holding the old header.
    fetch: async (input, init) => {
      const headers = new Headers(init?.headers);
      headers.set("X-CSRF-Token", await fetchCsrfToken());
      return globalThis.fetch(input, { ...init, credentials: "same-origin", headers });
    },
  });
  const client = new Client({ name: "haku-console-browser", version: "1" }, { capabilities: {} });
  await client.connect(transport);
  return client;
}

async function operatorMcpClient(): Promise<Client> {
  connectedClient ??= connectOperatorMcp().catch((error: unknown) => {
    connectedClient = null;
    throw error;
  });
  return connectedClient;
}

/** Invoke a proxied tool through the browser session on the console's canonical `/mcp` endpoint.
 * The server recognizes this as an Operator call, executes directly, and creates no approval row. */
export async function callOperatorMcpTool(name: string, args: Record<string, unknown>): Promise<unknown> {
  let result: unknown;
  try {
    result = await (await operatorMcpClient()).callTool({ name, arguments: args });
  } catch (error) {
    connectedClient = null;
    // Confirm whether the browser session expired. The shared OpenAPI response hook performs the
    // redirect on 401; when the session is healthy this cheap probe succeeds and the real MCP/upstream
    // error remains visible to the preview.
    try {
      await refreshCsrfToken();
    } catch {
      redirectToLogin();
    }
    throw error;
  }
  const error = mcpToolError(result);
  if (error !== null) throw new Error(error);
  return unwrapMcpToolResult(result);
}
