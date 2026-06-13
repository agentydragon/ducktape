/**
 * Bundles the Manifold MCP bridge into small executable files for the image.
 *
 * The runtime image only needs Node.js. supergateway and manifold-mcp-server
 * are bundled separately because supergateway starts the stdio server via a
 * shell command for each stateful HTTP session.
 */
import { chmod, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import esbuild from "esbuild";

const [bridgeOut, manifoldOut, supergatewayOut] = process.argv.slice(2);
if (!bridgeOut || !manifoldOut || !supergatewayOut) {
  console.error("Usage: bundle_bridge.mjs <bridge-out> <manifold-out> <supergateway-out>");
  process.exit(1);
}

const nodePaths = [resolve(process.cwd(), "node_modules")];
const shebang = "#!/usr/bin/env node";
const require = createRequire(import.meta.url);

async function bundle(entryPoint, outfile, options = {}) {
  await mkdir(dirname(outfile), { recursive: true });
  await esbuild.build({
    entryPoints: [require.resolve(entryPoint)],
    bundle: true,
    platform: "node",
    target: "node20",
    format: "esm",
    outfile,
    banner: options.banner ? { js: options.banner } : undefined,
    nodePaths,
    preserveSymlinks: false,
    logLevel: "info",
  });
  await chmod(outfile, 0o555);
}

const bridge = `${shebang}
const { spawn } = require("node:child_process");
const { dirname, join } = require("node:path");

const appDir = __dirname;
const node = process.execPath;
const supergatewayPath = join(appDir, "supergateway.mjs");
const manifoldPath = join(appDir, "manifold-mcp-server.mjs");
const port = process.env.PORT ?? "8080";

const shellQuote = (value) => "'" + value.replace(/'/g, "'\\\\''") + "'";

const child = spawn(
  node,
  [
    supergatewayPath,
    "--stdio",
    \`\${shellQuote(node)} \${shellQuote(manifoldPath)}\`,
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
`;

await mkdir(dirname(bridgeOut), { recursive: true });
await Promise.all([
  bundle("manifold-mcp-server/build/index.js", manifoldOut),
  bundle("supergateway/dist/index.js", supergatewayOut, {
    banner:
      "import { createRequire as __createRequire } from 'node:module';\n" +
      "const require = __createRequire(import.meta.url);",
  }),
  writeFile(bridgeOut, bridge),
]);
await chmod(bridgeOut, 0o555);
