import { createReadStream, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { extname, isAbsolute, join, normalize, resolve, sep } from "node:path";
import { createServer as createHttpsServer } from "node:https";
import { Agent as HttpsAgent } from "node:https";

import forge from "node-forge";
import { Proxy } from "http-mitm-proxy";
import { loadVendorRuntimeIndex, resolveVendorRuntimeRequest } from "./vendor_runtime.mjs";
import { requireValue, resolveWorkspacePath } from "./io.mjs";

const MODULE_SCRIPT_RE =
  /<script\b(?=[^>]*\btype\s*=\s*["']module["'])(?=[^>]*\bsrc\s*=\s*["'][^"']+["'])[^>]*>\s*<\/script>/gi;
const MODULE_PRELOAD_RE =
  /<link\b(?=[^>]*\brel\s*=\s*["'][^"']*\bmodulepreload\b[^"']*["'])(?=[^>]*\bhref\s*=\s*["'][^"']+["'])[^>]*>/gi;

const DEFAULT_APP_MANIFEST = null;
const DEFAULT_PROXY_HOST = "127.0.0.1";
const DEFAULT_PROXY_PORT = 8866;
const DEFAULT_ASSET_HOST = "127.0.0.1";
const DEFAULT_ASSET_PORT = 8867;

export function parseLiveProxyArgs(argv) {
  const options = {
    appManifestPath: DEFAULT_APP_MANIFEST,
    assetHost: DEFAULT_ASSET_HOST,
    assetPort: DEFAULT_ASSET_PORT,
    help: false,
    internalPrefix: undefined,
    packageRoots: undefined,
    packagesRoot: undefined,
    proxyHost: DEFAULT_PROXY_HOST,
    proxyPort: DEFAULT_PROXY_PORT,
    stateDir: undefined,
  };

  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    switch (arg) {
      case "--app-manifest":
        options.appManifestPath = resolveWorkspacePath(requireValue(argv, ++index, arg));
        break;
      case "--asset-host":
        options.assetHost = requireValue(argv, ++index, arg);
        break;
      case "--asset-port":
        options.assetPort = parsePort(requireValue(argv, ++index, arg), arg);
        break;
      case "--help":
      case "-h":
        options.help = true;
        break;
      case "--internal-prefix":
        options.internalPrefix = requireValue(argv, ++index, arg);
        break;
      case "--package-root": {
        const { packageName, packageRoot } = parsePackageRootArg(requireValue(argv, ++index, arg), arg);
        options.packageRoots ??= {};
        options.packageRoots[packageName] = resolvePath(packageRoot);
        break;
      }
      case "--packages-root":
        options.packagesRoot = resolvePath(requireValue(argv, ++index, arg));
        break;
      case "--proxy-host":
        options.proxyHost = requireValue(argv, ++index, arg);
        break;
      case "--proxy-port":
        options.proxyPort = parsePort(requireValue(argv, ++index, arg), arg);
        break;
      case "--state-dir":
        options.stateDir = resolvePath(requireValue(argv, ++index, arg));
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!options.help) {
    if (!options.appManifestPath) {
      throw new Error("--app-manifest is required");
    }
    options.appManifestPath = resolvePath(options.appManifestPath);
    options.stateDir = resolvePath(options.stateDir ?? defaultStateDir());
  }
  return options;
}

function formatLiveProxyHelp() {
  return [
    "Usage: bazel run //devinfra/js/debundle/live_proxy:serve_bin -- [options]",
    "",
    "Options:",
    "  --app-manifest <path>   App manifest to mount",
    `  --proxy-host <host>     MITM proxy listen host (default: ${DEFAULT_PROXY_HOST})`,
    `  --proxy-port <port>     MITM proxy listen port (default: ${DEFAULT_PROXY_PORT})`,
    `  --asset-host <host>     Local HTTPS asset server host (default: ${DEFAULT_ASSET_HOST})`,
    `  --asset-port <port>     Local HTTPS asset server port (default: ${DEFAULT_ASSET_PORT})`,
    "  --internal-prefix <p>   Internal same-origin prefix used for local JS assets",
    "  --package-root <p>=<d>  Explicit package dir for swapped vendor chunks (repeatable)",
    "  --packages-root <path>  Package tree root for swapped vendor chunks",
    "  --state-dir <path>      Cache/certificate directory",
    "  --help                  Show this message",
    "",
    "Manual browser flow:",
    "  1. Start the proxy.",
    "  2. Launch a dedicated Chrome/Chromium profile with --proxy-server=http://127.0.0.1:8866",
    "     and, for a quick local smoke test, --ignore-certificate-errors.",
    "  3. Open the printed target URL and sign in normally.",
  ].join("\n");
}

export function loadLiveProxyConfiguration(rawOptions) {
  const options = {
    ...rawOptions,
    appManifestPath: resolvePath(rawOptions.appManifestPath),
    packageRoots: rawOptions.packageRoots ? resolvePackageRoots(rawOptions.packageRoots) : undefined,
    packagesRoot: rawOptions.packagesRoot ? resolvePath(rawOptions.packagesRoot) : undefined,
    stateDir: resolvePath(rawOptions.stateDir ?? defaultStateDir()),
  };

  const appManifest = JSON.parse(readFileSync(options.appManifestPath, "utf8"));
  const manifestContext = {
    appManifest,
    appManifestPath: options.appManifestPath,
  };
  const assetSummaryPath = resolveManifestReferencedPath(appManifest.assetSummaryPath, manifestContext);
  const assetSummary = JSON.parse(readFileSync(assetSummaryPath, "utf8"));
  const sourceHtmlPath = resolveManifestReferencedPath(appManifest.sourceHtml, manifestContext);
  const sourceHtml = readFileSync(sourceHtmlPath, "utf8");
  const targetUrl = new URL(
    resolveAppBaseUrl({ appManifest, assetSummary, manifestContext }) ?? "https://example.test"
  );
  const uiVersion = appManifest.uiVersion ?? assetSummary.uiVersion ?? "unknown";
  const internalPrefix = normalizeInternalPrefix(
    options.internalPrefix ?? `${targetUrl.pathname.replace(/\/$/, "")}/_debundle/live/${uiVersion}`
  );
  const appRoot = resolveManifestReferencedPath(appManifest.outDir, manifestContext);
  const appAssetPrefix = `${internalPrefix}/app`;
  const vendorManifestPath = appManifest.vendorManifestPath
    ? resolveManifestReferencedPath(appManifest.vendorManifestPath, manifestContext)
    : join(appRoot, "vendors", "manifest.json");
  const vendorRuntimeIndex = loadVendorRuntimeIndex({
    manifestPath: vendorManifestPath,
    outRoot: appRoot,
    ...(options.packageRoots ? { packageRoots: options.packageRoots } : {}),
    ...(options.packagesRoot ? { packagesRoot: options.packagesRoot } : {}),
  });
  const bootstrapPath = join(appRoot, "bootstrap.js");
  if (!existsSync(bootstrapPath)) {
    throw new Error(`Expected bootstrap.js at ${bootstrapPath}`);
  }

  return {
    appAssetPrefix,
    appManifest,
    appManifestPath: options.appManifestPath,
    appRoot,
    assetHost: options.assetHost ?? DEFAULT_ASSET_HOST,
    assetPort: options.assetPort ?? DEFAULT_ASSET_PORT,
    assetSummary,
    assetSummaryPath,
    bootstrapUrl: `${appAssetPrefix}/bootstrap.js`,
    caDir: join(options.stateDir, "mitm-ca"),
    controlPaths: {
      liveIndex: `${internalPrefix}/live-index.html`,
      serviceWorker: `${internalPrefix}/sw.js`,
    },
    injectedHtml: rewriteHtmlForLiveProxy(sourceHtml, {
      bootstrapUrl: `${appAssetPrefix}/bootstrap.js`,
      uiVersion,
    }),
    internalPrefix,
    outRoot: appRoot,
    profileDir: join(options.stateDir, "browser-profile"),
    proxyHost: options.proxyHost ?? DEFAULT_PROXY_HOST,
    proxyPort: options.proxyPort ?? DEFAULT_PROXY_PORT,
    sourceHtmlPath,
    stateDir: options.stateDir,
    targetHost: targetUrl.host,
    targetOrigin: targetUrl.origin,
    targetUrl: targetUrl.href,
    uiVersion,
    vendorManifestPath,
    vendorRuntimeIndex,
  };
}

function resolveAppBaseUrl({ appManifest, assetSummary, manifestContext }) {
  if (assetSummary.baseUrl) {
    return assetSummary.baseUrl;
  }
  if (appManifest.baseUrl) {
    return appManifest.baseUrl;
  }
  const sourceMetadataPath = join(resolveManifestReferencedPath(appManifest.outDir, manifestContext), "SOURCE.json");
  if (!existsSync(sourceMetadataPath)) {
    return null;
  }
  const sourceMetadata = JSON.parse(readFileSync(sourceMetadataPath, "utf8"));
  return sourceMetadata.baseUrl ?? null;
}

function resolveManifestReferencedPath(value, { appManifest, appManifestPath }) {
  if (isAbsolute(value)) {
    return resolvePath(value);
  }
  const workspaceRoot = deriveManifestWorkspaceRoot(appManifestPath, appManifest);
  if (workspaceRoot) {
    return resolve(workspaceRoot, value);
  }
  return resolveWorkspacePath(value);
}

function deriveManifestWorkspaceRoot(appManifestPath, appManifest) {
  if (!appManifestPath || !appManifest?.outDir) {
    return null;
  }
  const normalizedManifestPath = resolvePath(appManifestPath).split(sep).join("/");
  const normalizedOutDir = normalizeRelativePath(appManifest.outDir);
  const suffix = `/${normalizedOutDir}/manifest.json`;
  if (!normalizedManifestPath.endsWith(suffix)) {
    return null;
  }
  return normalizedManifestPath.slice(0, -suffix.length);
}

function normalizeRelativePath(value) {
  return value
    .split(/[\\/]+/)
    .filter((segment) => segment !== "")
    .join("/");
}

export function rewriteHtmlForLiveProxy(sourceHtml, { bootstrapUrl, uiVersion }) {
  let html = sourceHtml.replace(MODULE_SCRIPT_RE, "");
  html = html.replace(MODULE_PRELOAD_RE, "");

  const injected = `${liveProxyPreludeScript({ bootstrapUrl, uiVersion })}
    <script type="module" crossorigin src="${escapeHtmlAttr(bootstrapUrl)}"></script>`;

  if (/<\/body>/i.test(html)) {
    html = html.replace(/<\/body>/i, `    ${injected}\n  </body>`);
  } else {
    html = `${html}\n${injected}\n`;
  }

  const comment = "Generated by //devinfra/js/debundle/live_proxy:serve_bin.";
  if (!html.includes(comment)) {
    html = html.replace(/<head>/i, `<head>\n    <!-- ${comment} -->`);
  }
  return html.endsWith("\n") ? html : `${html}\n`;
}

export function isTargetDocumentRequest(request, config) {
  const host = normalizeHost(request.headers?.host ?? "");
  if (host !== normalizeHost(config.targetHost)) {
    return false;
  }
  const destination = `${request.headers?.["sec-fetch-dest"] ?? ""}`.toLowerCase();
  if (destination === "document" || destination === "iframe") {
    return request.method === "GET";
  }
  const accept = `${request.headers?.accept ?? ""}`.toLowerCase();
  return request.method === "GET" && accept.includes("text/html");
}

export function mapLocalAssetPath(pathname, config) {
  const normalizedPath = pathname.split("?", 1)[0];
  if (!normalizedPath.startsWith(`${config.internalPrefix}/`)) {
    return null;
  }
  if (normalizedPath === config.controlPaths.liveIndex) {
    return {
      body: Buffer.from(config.injectedHtml, "utf8"),
      contentType: "text/html; charset=utf-8",
      kind: "live-index",
    };
  }
  if (normalizedPath === config.controlPaths.serviceWorker) {
    return {
      body: Buffer.from(noopServiceWorkerSource(), "utf8"),
      contentType: "text/javascript; charset=utf-8",
      kind: "service-worker",
    };
  }

  const suffix = decodeURIComponent(normalizedPath.slice(config.internalPrefix.length + 1));
  const vendorRuntime = resolveVendorRuntimeRequest(suffix, config.vendorRuntimeIndex);
  if (vendorRuntime) {
    return {
      chunkId: vendorRuntime.chunkId,
      contentType: contentTypeForPath(vendorRuntime.filePath),
      filePath: vendorRuntime.filePath,
      kind: "vendor-file",
    };
  }
  const appRelativePath = stripAppAssetPrefix(suffix);
  if (appRelativePath === null) {
    return null;
  }
  const filePath = safeJoin(config.appRoot ?? config.outRoot, appRelativePath);
  return {
    contentType: contentTypeForPath(filePath),
    filePath,
    kind: "file",
  };
}

function stripAppAssetPrefix(relativePath) {
  const normalized = normalizeRelativePath(relativePath);
  if (normalized === "app") {
    return "";
  }
  if (!normalized.startsWith("app/")) {
    return null;
  }
  return normalized.slice("app/".length);
}

export async function startLiveProxy(rawOptions) {
  const config = loadLiveProxyConfiguration(rawOptions);
  mkdirSync(config.stateDir, { recursive: true });
  mkdirSync(config.caDir, { recursive: true });

  const tlsMaterial = ensureHttpsAssetCertificate(config.stateDir);
  const assetServer = createHttpsAssetServer(config, tlsMaterial);
  await listenServer(assetServer, config.assetPort, config.assetHost);

  const proxy = new Proxy();
  const assetAgent = new HttpsAgent({
    keepAlive: true,
    rejectUnauthorized: false,
  });

  proxy.onError((ctx, err, errorKind) => {
    const url = ctx?.clientToProxyRequest?.url ?? "<unknown>";
    logWithTimestamp("error", `${errorKind ?? "PROXY_ERROR"} url=${url} message=${err?.message ?? err ?? "unknown"}`);
  });

  proxy.onRequest((ctx, callback) => {
    try {
      const requestUrl = parseProxyRequestUrl(ctx.clientToProxyRequest.url, ctx.clientToProxyRequest.headers.host);
      const host = normalizeHost(ctx.clientToProxyRequest.headers.host ?? "");
      if (host !== normalizeHost(config.targetHost)) {
        return callback();
      }

      if (requestUrl.pathname === "/sw.js") {
        retargetRequestToAssetServer(ctx, config, config.controlPaths.serviceWorker, assetAgent);
        logWithTimestamp("info", `override service-worker path=${requestUrl.pathname}`);
        return callback();
      }

      if (requestUrl.pathname.startsWith(`${config.internalPrefix}/`)) {
        retargetRequestToAssetServer(ctx, config, requestUrl.pathname + requestUrl.search, assetAgent);
        logWithTimestamp("info", `override asset path=${requestUrl.pathname}`);
        return callback();
      }

      if (isTargetDocumentRequest(ctx.clientToProxyRequest, config)) {
        retargetRequestToAssetServer(ctx, config, config.controlPaths.liveIndex, assetAgent);
        logWithTimestamp("info", `override document path=${requestUrl.pathname}`);
        return callback();
      }

      return callback();
    } catch (error) {
      return callback(error);
    }
  });

  await new Promise((resolve) => {
    proxy.listen(
      {
        forceSNI: true,
        host: config.proxyHost,
        keepAlive: true,
        port: config.proxyPort,
        sslCaDir: config.caDir,
      },
      resolve
    );
  });

  printStartupSummary(config);

  return {
    assetServer,
    close: async () => {
      proxy.close();
      await closeServer(assetServer);
    },
    config,
    proxy,
  };
}

function createHttpsAssetServer(config, tlsMaterial) {
  return createHttpsServer(
    {
      cert: tlsMaterial.certPem,
      key: tlsMaterial.keyPem,
    },
    (request, response) => {
      try {
        const requestUrl = new URL(request.url ?? "/", `https://${config.assetHost}:${config.assetPort}`);
        const resolved = mapLocalAssetPath(requestUrl.pathname, config);
        if (!resolved) {
          response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
          response.end("unknown local asset\n");
          return;
        }

        response.setHeader("cache-control", "no-store");
        response.setHeader("content-type", resolved.contentType);
        if (resolved.kind === "live-index" || resolved.kind === "service-worker") {
          response.writeHead(200);
          response.end(resolved.body);
          return;
        }

        if (!existsSync(resolved.filePath) || !statSync(resolved.filePath).isFile()) {
          response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
          response.end(`missing local asset: ${resolved.filePath}\n`);
          logWithTimestamp("warn", `missing local asset path=${requestUrl.pathname} file=${resolved.filePath}`);
          return;
        }

        response.writeHead(200);
        createReadStream(resolved.filePath).pipe(response);
      } catch (error) {
        response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
        response.end(`${error?.stack ?? error}\n`);
      }
    }
  );
}

function retargetRequestToAssetServer(ctx, config, path, assetAgent) {
  ctx.proxyToServerRequestOptions = {
    ...ctx.proxyToServerRequestOptions,
    agent: assetAgent,
    headers: {
      ...ctx.proxyToServerRequestOptions.headers,
      host: `${config.assetHost}:${config.assetPort}`,
    },
    host: config.assetHost,
    path,
    port: config.assetPort,
  };
}

function parseProxyRequestUrl(rawUrl, hostHeader) {
  if (/^https?:\/\//i.test(rawUrl ?? "")) {
    return new URL(rawUrl);
  }
  const host = normalizeHost(hostHeader ?? "localhost");
  return new URL(rawUrl ?? "/", `https://${host}`);
}

function ensureHttpsAssetCertificate(stateDir) {
  const certDir = join(stateDir, "asset-server-cert");
  const keyPath = join(certDir, "localhost-key.pem");
  const certPath = join(certDir, "localhost-cert.pem");
  if (existsSync(keyPath) && existsSync(certPath)) {
    return {
      certPem: readFileSync(certPath, "utf8"),
      keyPem: readFileSync(keyPath, "utf8"),
    };
  }

  mkdirSync(certDir, { recursive: true });
  const pki = forge.pki;
  const keys = pki.rsa.generateKeyPair(2048);
  const cert = pki.createCertificate();
  cert.publicKey = keys.publicKey;
  cert.serialNumber = `${Date.now()}`;
  cert.validity.notBefore = new Date(Date.now() - 60_000);
  cert.validity.notAfter = new Date(Date.now() + 3650 * 24 * 60 * 60 * 1000);
  const attrs = [{ name: "commonName", value: "localhost" }];
  cert.setSubject(attrs);
  cert.setIssuer(attrs);
  cert.setExtensions([
    { cA: false, name: "basicConstraints" },
    { digitalSignature: true, keyEncipherment: true, name: "keyUsage" },
    { name: "extKeyUsage", serverAuth: true },
    {
      altNames: [
        { type: 2, value: "localhost" },
        { ip: "127.0.0.1", type: 7 },
      ],
      name: "subjectAltName",
    },
  ]);
  cert.sign(keys.privateKey, forge.md.sha256.create());

  const keyPem = pki.privateKeyToPem(keys.privateKey);
  const certPem = pki.certificateToPem(cert);
  writeFileSync(keyPath, keyPem);
  writeFileSync(certPath, certPem);
  return { certPem, keyPem };
}

function liveProxyPreludeScript({ bootstrapUrl, uiVersion }) {
  const registrationLiteral = JSON.stringify({
    active: null,
    installing: null,
    scope: "/",
    waiting: null,
  });

  return `<script>
      (() => {
        const tag = "[js-debundle-live-proxy]";
        const noopRegistration = {
          ...${registrationLiteral},
          addEventListener() {},
          async unregister() { return true; },
          async update() {},
        };
        const serviceWorkerStub = {
          controller: null,
          ready: Promise.resolve(noopRegistration),
          addEventListener() {},
          removeEventListener() {},
          register: async () => noopRegistration,
          getRegistration: async () => undefined,
          getRegistrations: async () => [],
          startMessages() {},
        };
        globalThis.__jsDebundleLiveProxy = {
          active: true,
          bootstrapUrl: ${JSON.stringify(bootstrapUrl)},
          uiVersion: ${JSON.stringify(uiVersion)},
        };
        document.documentElement.dataset.jsDebundleLiveProxy = "true";
        const existing = navigator.serviceWorker;
        if (existing && typeof existing.getRegistrations === "function") {
          existing.getRegistrations().then((registrations) => {
            for (const registration of registrations) {
              registration.unregister().catch(() => {});
            }
          }).catch(() => {});
        }
        try {
          Object.defineProperty(navigator, "serviceWorker", {
            configurable: true,
            value: serviceWorkerStub,
          });
        } catch (error) {
          if (existing) {
            try { existing.register = serviceWorkerStub.register; } catch {}
            try { existing.getRegistration = serviceWorkerStub.getRegistration; } catch {}
            try { existing.getRegistrations = serviceWorkerStub.getRegistrations; } catch {}
            try { existing.ready = serviceWorkerStub.ready; } catch {}
          }
          console.warn(tag, "unable to replace navigator.serviceWorker directly", error);
        }
        console.info(tag, "active", globalThis.__jsDebundleLiveProxy);
      })();
    </script>`;
}

function noopServiceWorkerSource() {
  return [
    "self.addEventListener('install', (event) => {",
    "  self.skipWaiting();",
    "});",
    "self.addEventListener('activate', (event) => {",
    "  event.waitUntil(self.clients.claim());",
    "});",
    "self.addEventListener('fetch', () => {});",
    "",
  ].join("\n");
}

function safeJoin(root, relativePath) {
  const normalized = normalize(relativePath).replace(/^([/\\])+/, "");
  const resolvedPath = resolve(root, normalized);
  const normalizedRoot = `${resolve(root)}${root.endsWith("/") ? "" : "/"}`;
  if (!resolvedPath.startsWith(normalizedRoot) && resolvedPath !== resolve(root)) {
    throw new Error(`Refusing to serve path outside root: ${relativePath}`);
  }
  return resolvedPath;
}

function contentTypeForPath(path) {
  switch (extname(path)) {
    case ".css":
      return "text/css; charset=utf-8";
    case ".html":
      return "text/html; charset=utf-8";
    case ".js":
    case ".mjs":
      return "text/javascript; charset=utf-8";
    case ".json":
    case ".map":
      return "application/json; charset=utf-8";
    case ".svg":
      return "image/svg+xml";
    case ".txt":
      return "text/plain; charset=utf-8";
    default:
      return "application/octet-stream";
  }
}

function normalizeInternalPrefix(prefix) {
  const normalized = prefix.replace(/\/+$/, "");
  if (!normalized.startsWith("/")) {
    throw new Error(`Internal prefix must start with /, got ${prefix}`);
  }
  return normalized;
}

function normalizeHost(host) {
  return host.replace(/:\d+$/, "").toLowerCase();
}

function defaultStateDir() {
  return join("/tmp", "js-debundle-live-proxy");
}

function resolvePath(path) {
  return resolveWorkspacePath(path);
}

function resolvePackageRoots(packageRoots) {
  return Object.fromEntries(
    Object.entries(packageRoots).map(([packageName, packageRoot]) => [packageName, resolvePath(packageRoot)])
  );
}

function parsePackageRootArg(value, flag) {
  const separator = value.indexOf("=");
  if (separator <= 0 || separator === value.length - 1) {
    throw new Error(`${flag} must be in <package>=<dir> form, got ${value}`);
  }
  return {
    packageName: value.slice(0, separator),
    packageRoot: value.slice(separator + 1),
  };
}

function parsePort(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${flag} must be a valid TCP port, got ${value}`);
  }
  return parsed;
}

function logWithTimestamp(level, message) {
  const ts = new Date().toISOString();
  const stream = level === "error" ? process.stderr : process.stdout;
  stream.write(`${ts} [${level}] ${message}\n`);
}

function escapeHtmlAttr(value) {
  return value.replaceAll("&", "&amp;").replaceAll('"', "&quot;");
}

function printStartupSummary(config) {
  const quickStart = [
    "chromium",
    `--user-data-dir=${JSON.stringify(config.profileDir)}`,
    `--proxy-server=http://${config.proxyHost}:${config.proxyPort}`,
    "--ignore-certificate-errors",
    JSON.stringify(config.targetUrl),
  ].join(" ");

  logWithTimestamp(
    "info",
    `proxy ready target=${config.targetOrigin} listen=http://${config.proxyHost}:${config.proxyPort}`
  );
  logWithTimestamp("info", `local assets prefix=${config.internalPrefix}`);
  logWithTimestamp("info", `bootstrap override=${config.bootstrapUrl}`);
  logWithTimestamp("info", `mitm ca pem=${join(config.caDir, "certs", "ca.pem")}`);
  logWithTimestamp("info", `browser profile dir=${config.profileDir}`);
  logWithTimestamp("info", "quick-start browser command:");
  process.stdout.write(`${quickStart}\n`);
}

function listenServer(server, port, host) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => {
      server.off("error", reject);
      resolve();
    });
  });
}

function closeServer(server) {
  return new Promise((resolve, reject) => {
    server.close((error) => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

export async function runServeCli(argv) {
  const options = parseLiveProxyArgs(argv);
  if (options.help) {
    process.stdout.write(`${formatLiveProxyHelp()}\n`);
    return 0;
  }

  const handles = await startLiveProxy(options);
  const shutdown = async (signal) => {
    logWithTimestamp("info", `shutting down signal=${signal}`);
    try {
      await handles.close();
    } finally {
      process.exit(0);
    }
  };

  process.on("SIGINT", () => {
    void shutdown("SIGINT");
  });
  process.on("SIGTERM", () => {
    void shutdown("SIGTERM");
  });
  await new Promise(() => {});
}
