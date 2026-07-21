import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { mcpToolError, unwrapMcpToolResult } from "./mcp_result.ts";
import { redirectToOperatorLogin } from "./operator_login.ts";

let connectedClient: Promise<Client> | null = null;

async function connectOperatorMcp(): Promise<Client> {
  const transport = new StreamableHTTPClientTransport(new URL("/mcp", document.baseURI), {
    requestInit: { credentials: "same-origin" },
    fetch: async (input, init) => {
      const response = await globalThis.fetch(input, { ...init, credentials: "same-origin" });
      if (response.status === 401) redirectToOperatorLogin();
      return response;
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
  try {
    const result = await (await operatorMcpClient()).callTool({ name, arguments: args });
    const error = mcpToolError(result);
    if (error !== null) throw new Error(error);
    return unwrapMcpToolResult(result);
  } catch (error) {
    connectedClient = null;
    throw error;
  }
}
