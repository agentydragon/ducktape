#!/usr/bin/env node
// Development server: esbuild watch + HTTP server + backend

import { spawn } from "child_process";
import esbuild from "esbuild";
import { createServer, request } from "http";
import { readFile } from "fs/promises";
import { fileURLToPath } from "url";
import { delimiter, dirname, extname, join, resolve } from "path";
import { existsSync } from "fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
// Tailwind's CSS resolver reads NODE_PATH at module load; these are Bazel's linked npm trees.
const nodePaths = [resolve(__dirname, "node_modules"), resolve(__dirname, "../../node_modules")];
process.env.NODE_PATH = [...nodePaths, process.env.NODE_PATH].filter(Boolean).join(delimiter);
const { default: tailwindcss } = await import("esbuild-plugin-tailwindcss");

const FRONTEND_PORT = 5173;
const BACKEND_PORT = 8000;
const HOST = "localhost";

// Parse command line args
const args = process.argv.slice(2);
const skipBackend = args.includes("--frontend-only");

// Color codes for log prefixes
const C = {
  reset: "\x1b[0m",
  cyan: "\x1b[36m",
  yellow: "\x1b[33m",
  green: "\x1b[32m",
  dim: "\x1b[2m",
};

function prefixLines(prefix, color, data) {
  const lines = data
    .toString()
    .split("\n")
    .filter((l) => l.trim());
  for (const line of lines) {
    console.log(`${color}[${prefix}]${C.reset} ${line}`);
  }
}

// Health check with polling
async function waitForBackend(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`http://127.0.0.1:${BACKEND_PORT}/health`);
      if (res.ok) return true;
    } catch {
      // Not ready yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

const CONTENT_TYPES = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ico": "image/x-icon",
};

// Start esbuild in watch mode
const ctx = await esbuild.context({
  entryPoints: [resolve(__dirname, "src/main.tsx")],
  bundle: true,
  nodePaths,
  outdir: resolve(__dirname, "dist"),
  format: "esm",
  jsx: "automatic",
  splitting: true,
  sourcemap: true,
  target: ["es2022"],
  plugins: [tailwindcss()],
  alias: {
    $lib: resolve(__dirname, "src/lib"),
    $components: resolve(__dirname, "src/components"),
  },
  conditions: ["browser", "module", "import"],
  logLevel: "info",
});

await ctx.rebuild();
await ctx.watch();
console.log(`${C.yellow}[esbuild]${C.reset} watching for changes...`);

// Start backend process
let backendProcess = null;
if (!skipBackend) {
  // Check for required environment variables
  const REQUIRED_ENV = ["PGHOST", "PGPORT", "PGDATABASE", "PGUSER"];
  const missing = REQUIRED_ENV.filter((v) => !process.env[v]);
  if (missing.length > 0) {
    console.warn(`${C.yellow}Warning: Missing env vars: ${missing.join(", ")}${C.reset}`);
    console.warn(`${C.dim}Backend may fail. Run from props/ direnv shell.${C.reset}`);
  }

  const backendRlocation = process.env.PROPS_BACKEND_CLI_RLOCATION;
  const runfilesDir = process.env.JS_BINARY__RUNFILES;
  if (!backendRlocation || !runfilesDir) {
    throw new Error("Bazel did not provide the backend CLI runfiles path");
  }
  const backendBin = resolve(runfilesDir, backendRlocation);
  if (!existsSync(backendBin)) {
    throw new Error(`Backend CLI runfile was not found: ${backendBin}`);
  }
  const backendEnv = {
    ...process.env,
    RUNFILES_DIR: runfilesDir,
    JAVA_RUNFILES: runfilesDir,
  };
  // Use the same runfiles tree for the Python child as for this JS launcher.
  delete backendEnv.RUNFILES_MANIFEST_FILE;
  delete backendEnv.RUNFILES_MANIFEST_ONLY;

  backendProcess = spawn(backendBin, ["serve", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)], {
    stdio: ["ignore", "pipe", "pipe"],
    env: backendEnv,
  });

  backendProcess.stdout.on("data", (d) => prefixLines("backend", C.cyan, d));
  backendProcess.stderr.on("data", (d) => prefixLines("backend", C.cyan, d));

  backendProcess.on("error", (err) => {
    console.error(`${C.cyan}[backend]${C.reset} Failed to start: ${err.message}`);
  });

  backendProcess.on("exit", (code, signal) => {
    if (signal !== "SIGTERM" && signal !== "SIGINT" && code !== 0) {
      console.error(`${C.cyan}[backend]${C.reset} Process exited (code: ${code})`);
    }
  });
}

// Wait for backend health (if started)
let backendHealthy = skipBackend;
if (!skipBackend) {
  backendHealthy = await waitForBackend();
}

const DIST_ROOT = resolve(__dirname, "dist");
const FRONTEND_ROOT = resolve(__dirname);

function resolveSafe(root, urlPath) {
  // Walk path segments and reject anything that could escape `root`:
  // `..`, absolute segments, or null bytes. Then join — this avoids feeding
  // user input through `path.resolve`, which CodeQL flags as a sink.
  const segments = urlPath.split("/").filter((s) => s !== "" && s !== ".");
  for (const segment of segments) {
    if (segment === ".." || segment.includes("\0") || segment.includes("\\")) {
      return null;
    }
  }
  return segments.length === 0 ? root : join(root, ...segments);
}

// Start HTTP server
const server = createServer(async (req, res) => {
  let urlPath = req.url.split("?")[0];

  // The browser uses same-origin API and auth URLs. Forward them before the
  // SPA fallback so that the backend sees the original method, URL and headers.
  if (["/api", "/auth"].some((prefix) => urlPath === prefix || urlPath.startsWith(`${prefix}/`))) {
    const upstream = request(
      { hostname: "127.0.0.1", port: BACKEND_PORT, method: req.method, path: req.url, headers: req.headers },
      (response) => {
        res.writeHead(response.statusCode, response.headers);
        response.pipe(res);
      }
    );
    upstream.on("error", (err) => {
      console.error(`[dev-server] Backend request failed: ${err.message}`);
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
    req.pipe(upstream);
    return;
  }

  // Serve index.html for root and all routes (SPA behavior)
  if (urlPath === "/" || !urlPath.includes(".")) {
    urlPath = "/index.html";
  }

  // Try to serve from dist first, then from root. Reject path-traversal.
  let filePath = resolveSafe(DIST_ROOT, urlPath);
  if (filePath === null || !existsSync(filePath)) {
    filePath = resolveSafe(FRONTEND_ROOT, urlPath);
  }

  try {
    if (filePath === null) {
      const err = new Error("ENOENT");
      err.code = "ENOENT";
      throw err;
    }
    const content = await readFile(filePath);
    const ext = extname(filePath);
    res.setHeader("Content-Type", CONTENT_TYPES[ext] || "application/octet-stream");
    res.writeHead(200);
    res.end(content);
  } catch (err) {
    if (err.code === "ENOENT") {
      res.writeHead(404);
      res.end("Not found");
    } else {
      console.error("[dev-server] Unexpected error while serving request:", err);
      res.writeHead(500);
      res.end("Server error");
    }
  }
});

server.listen(FRONTEND_PORT, HOST, () => {
  console.log(`\n${"=".repeat(50)}`);
  console.log(`${C.green}Dev servers ready:${C.reset}`);
  console.log(`  Frontend: http://${HOST}:${FRONTEND_PORT}/`);
  if (!skipBackend) {
    const status = backendHealthy ? `${C.green}(healthy)${C.reset}` : `${C.yellow}(starting...)${C.reset}`;
    console.log(`  Backend:  http://127.0.0.1:${BACKEND_PORT}/ ${status}`);
  }
  console.log(`${"=".repeat(50)}\n`);
});

// Handle shutdown
process.on("SIGINT", async () => {
  console.log("\nShutting down...");
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill("SIGTERM");
  }
  await ctx.dispose();
  server.close();
  process.exit(0);
});

process.on("SIGTERM", async () => {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill("SIGTERM");
  }
  await ctx.dispose();
  server.close();
  process.exit(0);
});
