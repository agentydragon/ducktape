export type McpPreset = {
  id: string;
  label: string;
  transport: "stdio" | "sse" | "inproc" | "http";
  defaultName?: string;
  defaults: {
    stdio?: { command: string; args: any[]; env: Record<string, string> };
    sse?: {
      url: string;
      headers: Record<string, string>;
      timeout_secs: number;
      sse_read_timeout_secs: number;
    };
    inproc?: { factory: string; args: any[]; kwargs: Record<string, any> };
    http?: { url: string; headers?: Record<string, string>; auth?: string };
  };
};

export const MCP_PRESETS: McpPreset[] = [
  {
    id: "stdio_template",
    label: "Stdio Template",
    transport: "stdio",
    defaultName: "server",
    defaults: {
      stdio: {
        command: "/usr/bin/env",
        args: ["server-binary"],
        env: {},
      },
    },
  },
  {
    id: "http_template",
    label: "HTTP (manual)",
    transport: "http",
    defaultName: "server",
    defaults: {
      http: {
        url: "http://127.0.0.1:8768/mcp",
        headers: { Authorization: "Bearer <token>" },
      },
    },
  },
];
