import { MantineProvider } from "@mantine/core";
import { type ReactNode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, type Mock, vi } from "vitest";

import { api } from "../client";
import type { ActionGroupView, ActionRequestView, ActionState } from "./client";

/** Shared fixtures, stand-ins for the network, and mounting for the Action views' suites. */

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export const DECISION_NOTE = "test decision note";
export const ACTION_REQUEST_WATCH = "/upstream/actions/v1/action-requests:watch?state=ACTION_STATE_DECISION_PENDING";
export const ACTION_GROUP_WATCH = "/upstream/actions/v1/action-groups:watch";

export function request(state: ActionState, index: number): ActionRequestView {
  const decided = state !== "decision_pending";
  const executing = !["decision_pending", "allowed", "denied"].includes(state);
  return {
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    action: { group: "agentplane", name: `state-${state}` },
    arguments: { state, token: "test-exact-token", nested: { password: "test-exact-password" } },
    title: `test title for the ${state} fixture`,
    description: `test description adding what the ${state} title leaves out`,
    idempotency_key: `request-${index}`,
    caller: { namespace: "agentplane-test", name: "test-agent" },
    state,
    version: decided ? 2 : 1,
    created_at: "2026-09-05T12:00:00Z",
    updated_at: "2026-09-05T12:00:00Z",
    decision: decided
      ? {
          id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          verdict: state === "denied" ? "deny" : "allow",
          provider: "human_operator",
          operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
          decision_note: DECISION_NOTE,
          reason_code: null,
          reason_description: null,
          policy_evidence: null,
          idempotency_key: `decision-${index}`,
          decided_at: "2026-09-05T12:00:00Z",
        }
      : null,
    execution: executing
      ? {
          id: `30000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          state: state as NonNullable<ActionRequestView["execution"]>["state"],
          result: state === "succeeded" ? { ok: true } : null,
          error: ["failed", "cancelled", "execution_unknown"].includes(state) ? { kind: state } : null,
          created_at: "2026-09-05T12:00:00Z",
          started_at: "2026-09-05T12:00:01Z",
          reconciled_at: null,
          completed_at: ["running", "dispatching"].includes(state) ? null : "2026-09-05T12:00:02Z",
        }
      : null,
  };
}

/** Arguments the ssh group's `exec` Action takes. */
export const SSH_EXEC_ARGUMENTS: ActionRequestView["arguments"] = {
  host: "test-host.example",
  user: "test-user",
  command: "echo test-output",
};

/** The ssh group's `exec` Action in `state`. Once it ran, its result is stored as the Action Service
 * keeps an MCP tool's answer: the whole CallToolResult, where FastMCP writes the `ExecResult` the tool
 * returned twice, as structured content and as one JSON text block. `returned` overrides fields of
 * that `ExecResult`. */
export function sshExec(state: ActionState, returned: Record<string, unknown> = {}): ActionRequestView {
  const row = request(state, 1);
  const value = {
    host: "test-host.example",
    user: "test-user",
    exit_code: 0,
    stdout: "test-output\n",
    stderr: "",
    stdout_truncated: false,
    stderr_truncated: false,
    ...returned,
  };
  return {
    ...row,
    action: { group: "ssh", name: "exec" },
    arguments: SSH_EXEC_ARGUMENTS,
    execution: row.execution && {
      ...row.execution,
      result: row.execution.result && {
        content: [{ type: "text", text: JSON.stringify(value) }],
        structuredContent: value,
        isError: false,
      },
    },
  };
}

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement }> = [];

function toProtoJson(value: unknown, parentKey = "", ownerKey = ""): unknown {
  if (Array.isArray(value)) return value.map((item) => toProtoJson(item, parentKey, ownerKey));
  if (value === null || typeof value !== "object") {
    if (typeof value === "string") {
      const prefix =
        parentKey === "state"
          ? ownerKey === "execution"
            ? "EXECUTION_STATE_"
            : ownerKey === "health"
              ? "MCP_LIFECYCLE_"
              : "ACTION_STATE_"
          : parentKey === "verdict"
            ? "VERDICT_"
            : parentKey === "executor_kind"
              ? "EXECUTOR_KIND_"
              : parentKey === "source"
                ? "POLICY_SOURCE_"
                : parentKey === "type"
                  ? "POLICY_KIND_"
                  : parentKey === "reason" && ownerKey === "health"
                    ? "MCP_UNAVAILABLE_REASON_"
                    : undefined;
      if (prefix !== undefined) return `${prefix}${value.toUpperCase()}`;
      if (parentKey === "version" || parentKey === "generation") return value;
    }
    if (typeof value === "number" && (parentKey === "version" || parentKey === "generation")) {
      return String(value);
    }
    return value;
  }
  const dynamicFields = new Set(["arguments", "result", "error", "input_schema"]);
  return Object.fromEntries(
    Object.entries(value).map(([key, child]) => [
      key.replace(/_([a-z])/g, (_match, letter: string) => letter.toUpperCase()),
      dynamicFields.has(key) ? child : toProtoJson(child, key, parentKey),
    ])
  );
}

function actionResponse(path: string, value: unknown): unknown {
  if (value instanceof Response || value === undefined || value === null || typeof value !== "object") return value;
  if ("actionRequest" in value || "actionRequests" in value || "actionGroups" in value) return value;
  if (path.endsWith("/action-requests:history")) {
    const page = value as { items?: unknown[]; next_cursor?: string | null };
    return {
      actionRequests: (page.items ?? []).map((item) => toProtoJson(item)),
      nextCursor: page.next_cursor ?? undefined,
    };
  }
  if (path.endsWith("/action-requests")) {
    const rows = Array.isArray(value)
      ? value
      : Array.isArray((value as { items?: unknown }).items)
        ? (value as { items: unknown[] }).items
        : [];
    return { actionRequests: rows.map((item) => toProtoJson(item)) };
  }
  if (path.endsWith("/decision")) {
    return { actionRequest: toProtoJson(value) };
  }
  if (path.endsWith("/action-groups")) {
    const groups = Array.isArray(value) ? value : [];
    return { actionGroups: groups.map((item) => toProtoJson(item)) };
  }
  return { actionRequest: toProtoJson(value) };
}

/** A browser fetch stream that records the facade's NDJSON watch replay. */
export class TestNdjsonWatch {
  static readonly opened: Map<string, TestNdjsonWatch> = new Map();
  static current: TestNdjsonWatch | undefined;
  private controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  private readonly encoder = new TextEncoder();
  readonly response: Response;
  readonly url: string;
  readonly signal: AbortSignal | undefined;

  constructor(url: string, signal: AbortSignal | null | undefined) {
    this.url = url;
    this.signal = signal ?? undefined;
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
      cancel: () => {
        this.controller = undefined;
      },
    });
    this.response = new Response(stream, { headers: { "content-type": "application/x-ndjson" } });
    TestNdjsonWatch.current = this;
    TestNdjsonWatch.opened.set(url, this);
    this.signal?.addEventListener("abort", () => this.close(), { once: true });
  }

  send(value: unknown): void {
    if (this.controller === undefined) return;
    const groupWatch = this.url.includes("action-groups:watch");
    const snapshot = groupWatch
      ? { actionGroups: (value as ActionGroupView[]).map((item) => toProtoJson(item)) }
      : {
          actionRequests: (value as ActionRequestView[]).map((item) => toProtoJson(item)),
        };
    this.controller.enqueue(this.encoder.encode(`${JSON.stringify({ result: { snapshot } })}\n`));
    this.controller.enqueue(this.encoder.encode(`${JSON.stringify({ result: { snapshotComplete: {} } })}\n`));
  }

  sendRaw(line: string): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(this.encoder.encode(`${line}\n`));
  }

  drop(): void {
    this.controller?.error(new Error("test stream dropped"));
    this.controller = undefined;
  }

  close(): void {
    this.controller?.close();
    this.controller = undefined;
  }
}

let actionAnswer: (sent: Sent) => unknown = () => undefined;
let actionFetchInstalled = false;
let originalFetch: typeof fetch = globalThis.fetch.bind(globalThis);

function installActionFetch(): void {
  if (actionFetchInstalled) return;
  actionFetchInstalled = true;
  originalFetch = globalThis.fetch.bind(globalThis);
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const rawUrl = typeof input === "string" ? input : input instanceof Request ? input.url : input.href;
    const url = new URL(rawUrl, "https://app.invalid");
    if (!url.pathname.startsWith("/upstream/actions/")) return originalFetch(input, init);
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (url.pathname.endsWith(":watch")) {
      const watch = new TestNdjsonWatch(`${url.pathname}${url.search}`, init?.signal);
      watch.send([]);
      return watch.response;
    }
    const requestBody =
      typeof init?.body === "string"
        ? init.body
        : input instanceof Request && input.body !== null
          ? await input.clone().text()
          : undefined;
    const sent: Sent = {
      method,
      path: url.pathname,
      query: url.searchParams,
      body: requestBody ? (JSON.parse(requestBody) as unknown) : undefined,
    };
    const answer = actionAnswer(sent);
    if (answer === undefined) throw new Error(`unexpected ${method} ${url.pathname}`);
    if (answer instanceof Response) return answer;
    return Response.json(actionResponse(url.pathname, answer));
  });
}

/** Mounts `node` in the app's Mantine provider, until the test ends. */
export async function mount(node: ReactNode): Promise<HTMLDivElement> {
  installActionFetch();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push({ root, container });
  await act(async () => {
    root.render(<MantineProvider env="test">{node}</MantineProvider>);
  });
  return container;
}

/** The browser's `EventSource` as a test drives it; `TestEventSource.current` is the latest opened. */
export class TestEventSource extends EventTarget {
  static readonly CLOSED = 2;
  static current: TestEventSource | undefined;
  static readonly opened: Map<string, TestEventSource> = new Map();
  readyState = 1;
  close: Mock<() => void> = vi.fn();

  constructor(readonly url: string) {
    super();
    TestEventSource.current = this;
    TestEventSource.opened.set(url, this);
  }
}

/** Pushes one snapshot down the latest stream, or the one opened on `url`; a string goes as the
 * frame's raw data. */
export async function send(value: unknown, url?: string): Promise<void> {
  const watchUrl =
    url === undefined
      ? TestNdjsonWatch.opened.has(ACTION_REQUEST_WATCH)
        ? ACTION_REQUEST_WATCH
        : [...TestNdjsonWatch.opened.keys()].at(-1)
      : url.includes("action-groups:watch")
        ? ACTION_GROUP_WATCH
        : url.includes("action-requests:watch") || url.startsWith("/upstream/actions/")
          ? ACTION_REQUEST_WATCH
          : undefined;
  const watch = watchUrl === undefined ? undefined : TestNdjsonWatch.opened.get(watchUrl);
  if (watch !== undefined && typeof value !== "string") {
    await act(async () => watch.send(value));
    return;
  }
  if (watch !== undefined && typeof value === "string") {
    await act(async () => watch.sendRaw(value));
    return;
  }
  const source = url === undefined ? TestEventSource.current : TestEventSource.opened.get(url);
  await act(async () => {
    source?.dispatchEvent(
      new MessageEvent("snapshot", { data: typeof value === "string" ? value : JSON.stringify(value) })
    );
  });
}

export interface Sent {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
}

const ejects: (() => void)[] = [];

/** Answers the app's API in place of the network until the test ends, recording what was sent. An
 * answer is a JSON body or a ready `Response`; `undefined` fails the request as unexpected. */
export function answerApi(answer: (sent: Sent) => unknown): Sent[] {
  const sent: Sent[] = [];
  actionAnswer = (entry) => {
    sent.push(entry);
    return actionResponse(entry.path, answer(entry));
  };
  installActionFetch();
  const middleware = {
    async onRequest({ request }: { request: Request }) {
      const url = new URL(request.url, "https://app.invalid");
      const text = await request.text();
      const entry = {
        method: request.method,
        path: url.pathname,
        query: url.searchParams,
        body: text ? (JSON.parse(text) as unknown) : undefined,
      };
      sent.push(entry);
      const answered = await answer(entry);
      if (answered === undefined) throw new Error(`unexpected ${entry.method} ${entry.path}`);
      return answered instanceof Response ? answered : Response.json(answered);
    },
  };
  api.use(middleware);
  ejects.push(() => api.eject(middleware));
  return sent;
}

/** Mounts `node` with the Action stream stubbed, then pushes `pending` down it. */
export async function renderLive(node: ReactNode, pending: readonly ActionRequestView[] = []): Promise<HTMLDivElement> {
  vi.stubGlobal("EventSource", TestEventSource);
  installActionFetch();
  const container = await mount(node);
  await send(pending);
  return container;
}

export function button(container: HTMLElement, label: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find(
    (candidate) => candidate.getAttribute("aria-label") === label || candidate.textContent?.includes(label)
  );
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

/** Unmounts the most recently `render()`ed view now, for a test that asserts cleanup behavior
 * (e.g. a live stream's `close()`) rather than waiting for the automatic `afterEach` below. */
export async function unmountLast(): Promise<void> {
  const item = mounted.pop();
  if (!item) return;
  await act(async () => item.root.unmount());
  item.container.remove();
}

afterEach(async () => {
  for (const item of mounted.splice(0)) {
    await act(async () => item.root.unmount());
    item.container.remove();
  }
  for (const eject of ejects.splice(0)) eject();
  TestEventSource.current = undefined;
  TestEventSource.opened.clear();
  for (const watch of TestNdjsonWatch.opened.values()) watch.close();
  TestNdjsonWatch.opened.clear();
  TestNdjsonWatch.current = undefined;
  actionAnswer = () => undefined;
  actionFetchInstalled = false;
  vi.unstubAllGlobals();
});
