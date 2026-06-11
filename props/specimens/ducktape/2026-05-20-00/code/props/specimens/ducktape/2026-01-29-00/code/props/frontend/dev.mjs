#!/usr/bin/env node
// Development server: esbuild watch + HTTP server + backend

import { spawn } from 'child_process';
import esbuild from 'esbuild';
import esbuildSvelte from 'esbuild-svelte';
import tailwindcss from 'esbuild-plugin-tailwindcss';
import { createServer } from 'http';
import { readFile } from 'fs/promises';
import { fileURLToPath } from 'url';
import { dirname, resolve, join, extname } from 'path';
import { existsSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const FRONTEND_PORT = 5173;
const BACKEND_PORT = 8000;
const HOST = 'localhost';

// Parse command line args
const args = process.argv.slice(2);
const skipBackend = args.includes('--frontend-only');

// Color codes for log prefixes
const C = {
  reset: '\x1b[0m',
  cyan: '\x1b[36m',
  yellow: '\x1b[33m',
  green: '\x1b[32m',
  dim: '\x1b[2m',
};

function prefixLines(prefix, color, data) {
  const lines = data
    .toString()
    .split('\n')
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

// Start backend process
let backendProcess = null;
if (!skipBackend) {
  // Check for required environment variables
  const REQUIRED_ENV = ['PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER'];
  const missing = REQUIRED_ENV.filter((v) => !process.env[v]);
  if (missing.length > 0) {
    console.warn(`${C.yellow}Warning: Missing env vars: ${missing.join(', ')}${C.reset}`);
    console.warn(`${C.dim}Backend may fail. Run from props/ direnv shell.${C.reset}`);
  }

  // Backend CLI is in runfiles at props/backend/backend_cli
  // From props/frontend (chdir location), go up to runfiles root then down to backend
  const backendBin = resolve(__dirname, '..', 'backend', 'backend_cli');

  backendProcess = spawn(backendBin, ['--host', '127.0.0.1', '--port', String(BACKEND_PORT)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env },
  });

  backendProcess.stdout.on('data', (d) => prefixLines('backend', C.cyan, d));
  backendProcess.stderr.on('data', (d) => prefixLines('backend', C.cyan, d));

  backendProcess.on('error', (err) => {
    console.error(`${C.cyan}[backend]${C.reset} Failed to start: ${err.message}`);
  });

  backendProcess.on('exit', (code, signal) => {
    if (signal !== 'SIGTERM' && signal !== 'SIGINT' && code !== 0) {
      console.error(`${C.cyan}[backend]${C.reset} Process exited (code: ${code})`);
    }
  });
}

const CONTENT_TYPES = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ico': 'image/x-icon',
};

// Start esbuild in watch mode
const ctx = await esbuild.context({
  entryPoints: [resolve(__dirname, 'src/main.ts')],
  bundle: true,
  outdir: resolve(__dirname, 'dist'),
  format: 'esm',
  splitting: true,
  sourcemap: true,
  target: ['es2022'],
  plugins: [
    esbuildSvelte({
      compilerOptions: {
        css: 'injected',
      },
    }),
    tailwindcss(),
  ],
  alias: {
    $lib: resolve(__dirname, 'src/lib'),
    $components: resolve(__dirname, 'src/components'),
  },
  // Support svelte package exports condition (required for svelte-data-table, svelte-markdown)
  conditions: ['svelte', 'browser', 'module', 'import'],
  logLevel: 'info',
  // Suppress source map warnings - Svelte 5 compiler generates invalid source maps
  // https://github.com/sveltejs/svelte/issues/16615
  logOverride: {
    'invalid-source-mappings': 'silent',
  },
});

await ctx.watch();
console.log(`${C.yellow}[esbuild]${C.reset} watching for changes...`);

// Wait for backend health (if started)
let backendHealthy = skipBackend;
if (!skipBackend) {
  backendHealthy = await waitForBackend();
}

// Start HTTP server
const server = createServer(async (req, res) => {
  let urlPath = req.url.split('?')[0];

  // Serve index.html for root and all routes (SPA behavior)
  if (urlPath === '/' || !urlPath.includes('.')) {
    urlPath = '/index.html';
  }

  // Try to serve from dist first, then from root
  let filePath = join(__dirname, 'dist', urlPath);
  if (!existsSync(filePath)) {
    filePath = join(__dirname, urlPath);
  }

  try {
    const content = await readFile(filePath);
    const ext = extname(filePath);
    res.setHeader('Content-Type', CONTENT_TYPES[ext] || 'application/octet-stream');
    res.writeHead(200);
    res.end(content);
  } catch (err) {
    if (err.code === 'ENOENT') {
      res.writeHead(404);
      res.end('Not found: ' + urlPath);
    } else {
      res.writeHead(500);
      res.end('Server error: ' + err.message);
    }
  }
});

server.listen(FRONTEND_PORT, HOST, () => {
  console.log(`\n${'='.repeat(50)}`);
  console.log(`${C.green}Dev servers ready:${C.reset}`);
  console.log(`  Frontend: http://${HOST}:${FRONTEND_PORT}/`);
  if (!skipBackend) {
    const status = backendHealthy ? `${C.green}(healthy)${C.reset}` : `${C.yellow}(starting...)${C.reset}`;
    console.log(`  Backend:  http://127.0.0.1:${BACKEND_PORT}/ ${status}`);
  }
  console.log(`${'='.repeat(50)}\n`);
});

// Handle shutdown
process.on('SIGINT', async () => {
  console.log('\nShutting down...');
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill('SIGTERM');
  }
  await ctx.dispose();
  server.close();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill('SIGTERM');
  }
  await ctx.dispose();
  server.close();
  process.exit(0);
});
