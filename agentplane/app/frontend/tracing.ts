import { context, ROOT_CONTEXT, trace, type Span, type SpanContext } from "@opentelemetry/api";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, WebTracerProvider } from "@opentelemetry/sdk-trace-web";

const tracer = trace.getTracer("agentplane-web");
const PENDING_PREFIX = "agentplane:session-create-trace:";
const MAX_PENDING_AGE_MS = 15 * 60 * 1000;

type PendingSessionSpan = {
  sessionId: string;
  sandboxName: string;
  startedAt: number;
  parent: SpanContext;
};

const activeSpans = new Map<string, Span>();
const spanTimeouts = new Map<string, ReturnType<typeof setTimeout>>();
let initialized = false;

function operationKey(sessionId: string): string {
  return `${PENDING_PREFIX}${sessionId}`;
}

function readPending(sessionId: string): PendingSessionSpan | null {
  try {
    const raw = sessionStorage.getItem(operationKey(sessionId));
    if (raw === null) return null;
    const value: unknown = JSON.parse(raw);
    if (typeof value !== "object" || value === null) return null;
    const pending = value as Partial<PendingSessionSpan>;
    const parent = pending.parent;
    if (
      pending.sessionId !== sessionId ||
      typeof pending.sandboxName !== "string" ||
      typeof pending.startedAt !== "number" ||
      Date.now() - pending.startedAt > MAX_PENDING_AGE_MS ||
      typeof parent !== "object" ||
      parent === null ||
      typeof parent.traceId !== "string" ||
      typeof parent.spanId !== "string" ||
      typeof parent.traceFlags !== "number"
    ) {
      sessionStorage.removeItem(operationKey(sessionId));
      return null;
    }
    return pending as PendingSessionSpan;
  } catch {
    return null;
  }
}

function resumeSpan(sessionId: string): Span | null {
  const pending = readPending(sessionId);
  if (!pending) return null;
  // A page reload ends the original JS span. Resume the same trace and start time so the exported
  // user wait still covers the click through the eventual Thread navigation.
  const parentContext = trace.setSpanContext(ROOT_CONTEXT, { ...pending.parent, isRemote: true });
  const span = tracer.startSpan(
    "agentplane.session.create.user",
    { startTime: new Date(pending.startedAt) },
    parentContext
  );
  span.setAttribute("sandbox.name", pending.sandboxName);
  span.setAttribute("session.id", pending.sessionId);
  activeSpans.set(sessionId, span);
  return span;
}

function expireSpan(sessionId: string): void {
  const span = activeSpans.get(sessionId);
  if (span) {
    span.setAttribute("agentplane.session_create.outcome", "still_pending");
    span.end();
    activeSpans.delete(sessionId);
  }
  spanTimeouts.delete(sessionId);
  try {
    sessionStorage.removeItem(operationKey(sessionId));
  } catch {
    // Storage can be unavailable in a restricted browser context; the in-memory span still ends.
  }
}

function armExpiration(sessionId: string, startedAt: number): void {
  const remaining = Math.max(0, startedAt + MAX_PENDING_AGE_MS - Date.now());
  const timeout = setTimeout(() => expireSpan(sessionId), remaining);
  spanTimeouts.set(sessionId, timeout);
}

/** Start at the Create click; this record contains trace identity and time only, never form data. */
export function beginSessionCreate(sessionId: string, sandboxName: string): void {
  const span = tracer.startSpan("agentplane.session.create.user");
  span.setAttribute("sandbox.name", sandboxName);
  span.setAttribute("session.id", sessionId);
  const startedAt = Date.now();
  const parent = span.spanContext();
  activeSpans.set(sessionId, span);
  try {
    sessionStorage.setItem(operationKey(sessionId), JSON.stringify({ sessionId, sandboxName, startedAt, parent }));
  } catch {
    // Keep the current-page operation traced even when the browser denies sessionStorage.
  }
  armExpiration(sessionId, startedAt);
}

/** Keep API spans under the user operation across async React work and a browser reload. */
export async function withinSessionCreate<T>(sessionId: string, operation: () => Promise<T>): Promise<T> {
  const span = activeSpans.get(sessionId) ?? resumeSpan(sessionId);
  if (!span) return operation();
  if (!spanTimeouts.has(sessionId)) {
    const pending = readPending(sessionId);
    if (pending) armExpiration(sessionId, pending.startedAt);
  }
  return await context.with(trace.setSpan(context.active(), span), operation);
}

/** End only after the resolved Thread is available and the app has initiated its navigation. */
export function finishSessionCreate(
  sessionId: string,
  outcome: "opened" | "failed" | "rejected" | "unknown" = "opened"
): void {
  const span = activeSpans.get(sessionId) ?? resumeSpan(sessionId);
  if (span) {
    span.setAttribute("agentplane.session_create.outcome", outcome);
    span.end();
    activeSpans.delete(sessionId);
  }
  const timeout = spanTimeouts.get(sessionId);
  if (timeout !== undefined) clearTimeout(timeout);
  spanTimeouts.delete(sessionId);
  try {
    sessionStorage.removeItem(operationKey(sessionId));
  } catch {
    // No persistent operation context remains in memory after this navigation.
  }
}

function normalizedPath(pathname: string): string {
  const sandbox = pathname.match(/^\/sandboxes\/[^/]+(.*)$/);
  if (sandbox) {
    const suffix = sandbox[1];
    if (suffix === "/sessions") return "/sandboxes/{name}/sessions";
    if (suffix === "/sessions/{session_id}" || /^\/sessions\/[^/]+$/.test(suffix)) {
      return "/sandboxes/{name}/sessions/{session_id}";
    }
    if (/^\/sessions\/[^/]+\/opening$/.test(suffix)) {
      return "/sandboxes/{name}/sessions/{session_id}/opening";
    }
    if (suffix === "/runner-status") return "/sandboxes/{name}/runner-status";
    if (suffix === "/notifications/stream") return "/sandboxes/{name}/notifications/stream";
    return "/sandboxes/{name}/<route>";
  }
  if (/^\/threads\/[^/]+(?:\/|$)/.test(pathname)) return "/threads/{thread_id}/<route>";
  if (/^\/actions\/[^/]+(?:\/|$)/.test(pathname)) return "/actions/{request_id}/<route>";
  if (pathname.startsWith("/upstream/")) return "/upstream/{service}/{route}";
  if (pathname === "/" || pathname === "/models" || pathname === "/presets" || pathname === "/sandboxes") {
    return pathname;
  }
  const root = pathname.split("/").filter(Boolean)[0];
  return root ? `/${root}/<route>` : "/<route>";
}

function safeUrl(request: Request | string | URL): URL {
  if (request instanceof URL) return request;
  if (request instanceof Request) return new URL(request.url);
  return new URL(request, document.baseURI);
}

/** Install browser trace export and same-origin W3C trace-context propagation once at startup. */
export async function initializeBrowserTracing(): Promise<void> {
  if (initialized) return;
  initialized = true;

  // Zone.js patches browser APIs at import time. Load it only in the real app entrypoint so
  // importing tracing helpers in component tests does not replace their fake browser APIs.
  const { ZoneContextManager } = await import("@opentelemetry/context-zone");
  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({ "service.name": "agentplane-web" }),
    spanProcessors: [
      new BatchSpanProcessor(
        new OTLPTraceExporter({ url: new URL("/telemetry/v1/traces", window.location.origin).toString() })
      ),
    ],
  });
  provider.register({
    contextManager: new ZoneContextManager(),
    propagator: new W3CTraceContextPropagator(),
  });
  const escapedOrigin = window.location.origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [
      new FetchInstrumentation({
        ignoreUrls: [/\/telemetry\/v1\/traces(?:\?.*)?$/],
        propagateTraceHeaderCorsUrls: [new RegExp(`^${escapedOrigin}(?:/|$)`, "i")],
        applyCustomAttributesOnSpan: (span, request) => {
          const url = safeUrl(request as Request | string | URL);
          const path = normalizedPath(url.pathname);
          const method = request instanceof Request ? request.method : "HTTP";
          span.updateName(`${method} ${path}`);
          span.setAttribute("http.method", method);
          span.setAttribute("http.url", path);
          span.setAttribute("url.full", path);
          span.setAttribute("url.path", path);
          span.setAttribute("url.query", "");
          span.setAttribute("http.target", path);
          span.setAttribute("server.address", "");
          span.setAttribute("server.port", 0);
        },
      }),
    ],
  });
}
