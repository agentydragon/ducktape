import { MantineProvider } from "@mantine/core";
import { type ReactNode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, type Mock, vi } from "vitest";

import { api } from "../../client";
import type { ActionGroupView, ActionRequestView, ActionState } from "../client";
import type { McpLinkageView } from "../../mcp_linkage_client";
import {
  actionGroupUpsertProtoJson,
  actionGroupsSnapshotCompleteProtoJson,
  actionGroupsSnapshotProtoJson,
  actionRequestUpsertProtoJson,
  actionRequestsSnapshotCompleteProtoJson,
  actionRequestsSnapshotProtoJson,
  decideActionRequestProtoJson,
  disconnectMcpLinkageProtoJson,
  getActionRequestProtoJson,
  listActionGroupsProtoJson,
  listActionHistoryProtoJson,
  listActionRequestsProtoJson,
  mcpLinkageUpsertProtoJson,
  mcpLinkagesSnapshotCompleteProtoJson,
  mcpLinkagesSnapshotProtoJson,
  startMcpLinkageProtoJson,
} from "./proto_fixtures";
import type { ActionHistoryPage } from "../types";

/** Shared fixtures, stand-ins for the network, and mounting for the Action views' suites. */

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export const DECISION_NOTE = "test decision note";
export const ACTION_REQUEST_WATCH = "/upstream/actions/v1/action-requests:watch?state=ACTION_STATE_DECISION_PENDING";
export const ACTION_GROUP_WATCH = "/upstream/actions/v1/action-groups:watch";
export const MCP_LINKAGE_WATCH = "/upstream/actions/v1/operator/mcp-servers:watch";

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

function actionResponse(path: string, value: unknown): unknown {
  if (value instanceof Response || value === undefined || value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) {
    if (path.endsWith("/action-groups")) {
      if (!value.every(isActionGroupView)) throw new Error("Expected ActionGroupView fixtures.");
      return listActionGroupsProtoJson(value);
    }
    if (path.endsWith("/action-requests")) {
      if (!value.every(isActionRequestView)) throw new Error("Expected ActionRequestView fixtures.");
      return listActionRequestsProtoJson(value);
    }
    return value;
  }
  if (!isRecord(value)) return value;
  if (path.endsWith("/action-requests:history")) {
    if (!isActionHistoryPage(value)) throw new Error("Expected an ActionHistoryPage fixture.");
    return listActionHistoryProtoJson(value);
  }
  if (path.endsWith("/decision")) {
    if (!isActionRequestView(value)) throw new Error("Expected an ActionRequestView decision fixture.");
    return decideActionRequestProtoJson(value);
  }
  if (path.endsWith("/action-groups")) {
    return value;
  }
  if (/\/action-requests\/[^/]+$/.test(path)) {
    if (!isActionRequestView(value)) throw new Error("Expected an ActionRequestView fixture.");
    return getActionRequestProtoJson(value);
  }
  if (path.endsWith("/linkage/start")) {
    if (typeof value.authorization_url !== "string") throw new Error("Expected an authorization URL fixture.");
    return startMcpLinkageProtoJson(value.authorization_url);
  }
  if (path.endsWith("/linkage/disconnect")) {
    if (!isMcpLinkageView(value.linkage)) throw new Error("Expected an MCP linkage fixture.");
    return disconnectMcpLinkageProtoJson(value.linkage);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isActionRequestView(value: unknown): value is ActionRequestView {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.idempotency_key === "string" &&
    isRecord(value.action) &&
    typeof value.state === "string" &&
    typeof value.version === "number" &&
    typeof value.created_at === "string" &&
    typeof value.updated_at === "string"
  );
}

function isActionGroupView(value: unknown): value is ActionGroupView {
  return (
    isRecord(value) &&
    typeof value.key === "string" &&
    typeof value.executor_kind === "string" &&
    Array.isArray(value.actions)
  );
}

function isMcpLinkageView(value: unknown): value is McpLinkageView {
  return (
    isRecord(value) &&
    typeof value.server_id === "string" &&
    typeof value.server_url === "string" &&
    typeof value.status === "string" &&
    Array.isArray(value.scopes)
  );
}

function isActionHistoryPage(value: Record<string, unknown>): value is Record<string, unknown> & ActionHistoryPage {
  return (
    Array.isArray(value.items) &&
    value.items.every(isActionRequestView) &&
    (value.next_cursor === null || typeof value.next_cursor === "string")
  );
}

type ActionFixture = ActionRequestView | ActionGroupView | McpLinkageView;

/** A browser fetch stream that records the facade's NDJSON watch replay. */
export class TestNdjsonWatch {
  static readonly opened: Map<string, TestNdjsonWatch> = new Map();
  static current: TestNdjsonWatch | undefined;
  private static readonly openWaiters = new Map<string, ((watch: TestNdjsonWatch) => void)[]>();
  private controller: ReadableStreamDefaultController<Uint8Array> | undefined;
  private readonly encoder = new TextEncoder();
  readonly response: Response;
  readonly url: string;
  readonly signal: AbortSignal | undefined;

  static waitForOpen(url: string): Promise<TestNdjsonWatch> {
    const opened = TestNdjsonWatch.opened.get(url);
    if (opened !== undefined) return Promise.resolve(opened);
    return new Promise((resolve) => {
      const waiters = TestNdjsonWatch.openWaiters.get(url) ?? [];
      waiters.push(resolve);
      TestNdjsonWatch.openWaiters.set(url, waiters);
    });
  }

  static reset(): void {
    TestNdjsonWatch.opened.clear();
    TestNdjsonWatch.openWaiters.clear();
    TestNdjsonWatch.current = undefined;
  }

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
    const waiters = TestNdjsonWatch.openWaiters.get(url);
    if (waiters !== undefined) {
      TestNdjsonWatch.openWaiters.delete(url);
      for (const resolve of waiters) resolve(this);
    }
    this.signal?.addEventListener("abort", () => this.close(), { once: true });
  }

  sendActionRequests(value: readonly ActionRequestView[]): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(
      this.encoder.encode(
        `${JSON.stringify({ result: actionRequestsSnapshotProtoJson(value) })}\n${JSON.stringify({ result: actionRequestsSnapshotCompleteProtoJson() })}\n`
      )
    );
  }

  sendActionGroups(value: readonly ActionGroupView[]): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(
      this.encoder.encode(
        `${JSON.stringify({ result: actionGroupsSnapshotProtoJson(value) })}\n${JSON.stringify({ result: actionGroupsSnapshotCompleteProtoJson() })}\n`
      )
    );
  }

  sendMcpLinkages(value: readonly McpLinkageView[]): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(
      this.encoder.encode(
        `${JSON.stringify({ result: mcpLinkagesSnapshotProtoJson(value) })}\n${JSON.stringify({ result: mcpLinkagesSnapshotCompleteProtoJson() })}\n`
      )
    );
  }

  sendUpsertFrames(value: readonly ReturnType<typeof actionRequestUpsertProtoJson>[]): void {
    if (this.controller === undefined) return;
    for (const frame of value) this.controller.enqueue(this.encoder.encode(`${JSON.stringify({ result: frame })}\n`));
  }

  sendGroupUpsertFrames(value: readonly ReturnType<typeof actionGroupUpsertProtoJson>[]): void {
    if (this.controller === undefined) return;
    for (const frame of value) this.controller.enqueue(this.encoder.encode(`${JSON.stringify({ result: frame })}\n`));
  }

  sendLinkageUpsertFrames(value: readonly ReturnType<typeof mcpLinkageUpsertProtoJson>[]): void {
    if (this.controller === undefined) return;
    for (const frame of value) this.controller.enqueue(this.encoder.encode(`${JSON.stringify({ result: frame })}\n`));
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

let actionAnswer: (sent: Sent) => unknown | Promise<unknown> = () => undefined;
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
    const answer = await actionAnswer(sent);
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
export async function send(value: string | readonly ActionFixture[], url?: string): Promise<void> {
  let watchUrl =
    url === undefined
      ? TestNdjsonWatch.opened.has(ACTION_REQUEST_WATCH)
        ? ACTION_REQUEST_WATCH
        : [...TestNdjsonWatch.opened.keys()].at(-1)
      : url.includes("action-groups:watch")
        ? ACTION_GROUP_WATCH
        : url.includes("mcp-servers:watch")
          ? MCP_LINKAGE_WATCH
          : url.includes("action-requests:watch") || url.startsWith("/upstream/actions/")
            ? ACTION_REQUEST_WATCH
            : undefined;
  let watch = watchUrl === undefined ? undefined : TestNdjsonWatch.opened.get(watchUrl);
  if (watch === undefined && TestEventSource.current === undefined) {
    watchUrl ??= url === undefined ? ACTION_REQUEST_WATCH : undefined;
    if (watchUrl !== undefined) watch = await TestNdjsonWatch.waitForOpen(watchUrl);
  }
  if (watch !== undefined && typeof value !== "string") {
    await act(async () => {
      if (watchUrl === ACTION_GROUP_WATCH) {
        const groups = value.map((item) => {
          if (!isActionGroupView(item)) throw new Error("Expected ActionGroupView fixtures.");
          return item;
        });
        watch.sendActionGroups(groups);
      } else if (watchUrl === MCP_LINKAGE_WATCH) {
        const linkages = value.map((item) => {
          if (!isMcpLinkageView(item)) throw new Error("Expected McpLinkageView fixtures.");
          return item;
        });
        watch.sendMcpLinkages(linkages);
      } else {
        const requests = value.map((item) => {
          if (!isActionRequestView(item)) throw new Error("Expected ActionRequestView fixtures.");
          return item;
        });
        watch.sendActionRequests(requests);
      }
      await Promise.resolve();
    });
    return;
  }
  if (watch !== undefined && typeof value === "string") {
    await act(async () => {
      watch.sendRaw(value);
      await Promise.resolve();
    });
    return;
  }
  const source = url === undefined ? TestEventSource.current : TestEventSource.opened.get(url);
  await act(async () => {
    source?.dispatchEvent(
      new MessageEvent("snapshot", { data: typeof value === "string" ? value : JSON.stringify(value) })
    );
  });
}

/** Pushes upsert frames down a generated watch, as the service does after its initial snapshot. */
export async function sendUpserts(values: readonly ActionFixture[], url: string): Promise<void> {
  const watchUrl = url.includes("action-groups:watch")
    ? ACTION_GROUP_WATCH
    : url.includes("mcp-servers:watch")
      ? MCP_LINKAGE_WATCH
      : url.includes("action-requests:watch")
        ? ACTION_REQUEST_WATCH
        : undefined;
  const watch = watchUrl === undefined ? undefined : TestNdjsonWatch.opened.get(watchUrl);
  if (watch === undefined) throw new Error(`No generated watch is open for ${url}`);
  await act(async () => {
    if (watchUrl === ACTION_GROUP_WATCH) {
      const groups = values.map((value) => {
        if (!isActionGroupView(value)) throw new Error("Expected ActionGroupView upserts.");
        return actionGroupUpsertProtoJson(value);
      });
      watch.sendGroupUpsertFrames(groups);
      return;
    }
    if (watchUrl === MCP_LINKAGE_WATCH) {
      const linkages = values.map((value) => {
        if (!isMcpLinkageView(value)) throw new Error("Expected McpLinkageView upserts.");
        return mcpLinkageUpsertProtoJson(value);
      });
      watch.sendLinkageUpsertFrames(linkages);
      return;
    }
    const requests = values.map((value) => {
      if (!isActionRequestView(value)) throw new Error("Expected ActionRequestView upserts.");
      return actionRequestUpsertProtoJson(value);
    });
    watch.sendUpsertFrames(requests);
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
  actionAnswer = async (entry) => {
    sent.push(entry);
    return answer(entry);
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
  TestNdjsonWatch.reset();
  actionAnswer = () => undefined;
  actionFetchInstalled = false;
  vi.unstubAllGlobals();
});
