import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  callTool: vi.fn(),
  connect: vi.fn(),
  fetchCsrfToken: vi.fn(),
  refreshCsrfToken: vi.fn(),
}));

vi.mock("@modelcontextprotocol/sdk/client/index.js", () => ({
  Client: class {
    async connect(transport: unknown): Promise<void> {
      await mocks.connect(transport);
    }

    async callTool(request: unknown): Promise<unknown> {
      return mocks.callTool(request);
    }
  },
}));

vi.mock("@modelcontextprotocol/sdk/client/streamableHttp.js", () => ({
  StreamableHTTPClientTransport: class {
    constructor(_url: URL, _options: unknown) {}
  },
}));

vi.mock("./client.ts", () => ({
  fetchCsrfToken: mocks.fetchCsrfToken,
  refreshCsrfToken: mocks.refreshCsrfToken,
}));

import { callOperatorMcpTool } from "./mcp_client.ts";

describe("Operator MCP client", () => {
  beforeEach(() => {
    mocks.callTool.mockReset();
    mocks.connect.mockReset();
    mocks.fetchCsrfToken.mockReset();
    mocks.refreshCsrfToken.mockReset();
  });

  it("keeps a healthy connection after an ordinary MCP tool error", async () => {
    mocks.callTool
      .mockResolvedValueOnce({ isError: true, content: [{ type: "text", text: "backend rejected input" }] })
      .mockResolvedValueOnce({ structuredContent: { value: 2 } });

    await expect(callOperatorMcpTool("server_mutate", { value: 1 })).rejects.toThrow("backend rejected input");
    await expect(callOperatorMcpTool("server_mutate", { value: 2 })).resolves.toEqual({ value: 2 });

    expect(mocks.connect).toHaveBeenCalledTimes(1);
    expect(mocks.refreshCsrfToken).not.toHaveBeenCalled();
  });
});
