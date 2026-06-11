// stdio→Streamable HTTP bridge for the manifold-mcp-server image.
//
// Wraps the stdio-only bmorphism/manifold-mcp-server npm package as a
// Streamable HTTP MCP endpoint (default path /mcp, port 8080) using
// supergateway. The mcp-oauth-facade sidecar then talks to it over plain HTTP
// on localhost:8080. MANIFOLD_API_KEY comes from the pod's env and is
// inherited by the spawned manifold-mcp-server child.

import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const supergatewayPath = require.resolve("supergateway/dist/index.js");
const manifoldPath = require.resolve("manifold-mcp-server/build/index.js");

const port = process.env.PORT ?? "8080";

// `--stateful` keeps a single stdio child per HTTP session (the default is
// per-request), so Initialize state survives across the facade's
// list_tools/call_tool calls in the same FastMCP ProxyClient session.
const child = spawn(
  "node",
  [
    supergatewayPath,
    "--stdio",
    `node ${manifoldPath}`,
    "--outputTransport",
    "streamableHttp",
    "--stateful",
    "--port",
    port,
  ],
  { stdio: "inherit" }
);

const forward = (signal) => () => child.kill(signal);
process.on("SIGTERM", forward("SIGTERM"));
process.on("SIGINT", forward("SIGINT"));
child.on("exit", (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
