import { MantineProvider } from "@mantine/core";
import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { type ReactNode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, vi } from "vitest";
import { create, fromJson, type JsonObject } from "@bufbuild/protobuf";
import { TimestampSchema, ValueSchema } from "@bufbuild/protobuf/wkt";

import { api } from "../../client";
import { createAppQueryClient } from "../../query";
import {
  ActionRequestSchema,
  ActionState,
  ActionIdentitySchema,
  DecisionSchema,
  ExecutionSchema,
  ExecutionState,
  Verdict,
  type ActionGroup,
  type ActionRequest,
  type ListActionRequestHistoryResponse,
} from "../../../../action_service/action_service_pb";
import type { McpServerLinkage } from "../../../../action_service/operator_action_service_pb";
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

/** Shared fixtures, stand-ins for the network, and mounting for the Action views' suites. */

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export const DECISION_NOTE = "test decision note";
export const ACTION_REQUEST_WATCH = "/upstream/actions/v1/action-requests:watch?state=ACTION_STATE_DECISION_PENDING";
export const ACTION_GROUP_WATCH = "/upstream/actions/v1/action-groups:watch";
export const MCP_LINKAGE_WATCH = "/upstream/actions/v1/operator/mcp-servers:watch";

function stateName(state: ActionState): string {
  return ActionState[state].replace("ACTION_STATE_", "").toLowerCase();
}

export function request(state: ActionState, index: number): ActionRequest {
  const name = stateName(state);
  const decided = state !== ActionState.DECISION_PENDING;
  const executionState = new Map<ActionState, ExecutionState>([
    [ActionState.DISPATCHING, ExecutionState.DISPATCHING],
    [ActionState.RUNNING, ExecutionState.RUNNING],
    [ActionState.SUCCEEDED, ExecutionState.SUCCEEDED],
    [ActionState.FAILED, ExecutionState.FAILED],
    [ActionState.CANCELLED, ExecutionState.CANCELLED],
    [ActionState.EXECUTION_UNKNOWN, ExecutionState.EXECUTION_UNKNOWN],
  ]).get(state);
  return create(ActionRequestSchema, {
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    action: create(ActionIdentitySchema, { group: "agentplane", name: `state-${name}` }),
    arguments: {
      state: name,
      token: "test-exact-token",
      nested: { password: "test-exact-password" },
    },
    title: `test title for the ${name} fixture`,
    description: `test description adding what the ${name} title leaves out`,
    idempotencyKey: `request-${index}`,
    caller: { namespace: "agentplane-test", name: "test-agent" },
    state,
    version: decided ? 2n : 1n,
    createdAt: fromJson(TimestampSchema, "2026-09-05T12:00:00Z"),
    updatedAt: fromJson(TimestampSchema, "2026-09-05T12:00:00Z"),
    decision: decided
      ? create(DecisionSchema, {
          id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          verdict: state === ActionState.DENIED ? Verdict.DENY : Verdict.ALLOW,
          provider: "human_operator",
          operator: { issuer: "https://test-operator.example/oidc", subject: "test-operator" },
          decisionNote: DECISION_NOTE,
          idempotencyKey: `decision-${index}`,
          decidedAt: fromJson(TimestampSchema, "2026-09-05T12:00:00Z"),
        })
      : undefined,
    execution:
      executionState === undefined
        ? undefined
        : create(ExecutionSchema, {
            id: `30000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
            state: executionState,
            result: state === ActionState.SUCCEEDED ? fromJson(ValueSchema, { ok: true }) : undefined,
            error: [ActionState.FAILED, ActionState.CANCELLED, ActionState.EXECUTION_UNKNOWN].includes(state)
              ? { kind: name }
              : undefined,
            createdAt: fromJson(TimestampSchema, "2026-09-05T12:00:00Z"),
            startedAt: fromJson(TimestampSchema, "2026-09-05T12:00:01Z"),
            completedAt:
              state === ActionState.RUNNING || state === ActionState.DISPATCHING
                ? undefined
                : fromJson(TimestampSchema, "2026-09-05T12:00:02Z"),
          }),
  });
}

/** Arguments the ssh group's `exec` Action takes. */
export const SSH_EXEC_ARGUMENTS: JsonObject = {
  host: "test-host.example",
  user: "test-user",
  command: "echo test-output",
};

/** The ssh group's `exec` Action in `state`. Once it ran, its result is stored as the Action Service
 * keeps an MCP tool's answer: the whole CallToolResult, where FastMCP writes the `ExecResult` the tool
 * returned twice, as structured content and as one JSON text block. `returned` overrides fields of
 * that `ExecResult`. */
export function sshExec(state: ActionState, returned: Record<string, unknown> = {}): ActionRequest {
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
  row.action = create(ActionIdentitySchema, { group: "ssh", name: "exec" });
  row.arguments = SSH_EXEC_ARGUMENTS;
  if (row.execution?.result !== undefined) {
    row.execution.result = fromJson(ValueSchema, {
      content: [{ type: "text", text: JSON.stringify(value) }],
      structuredContent: value,
      isError: false,
    });
  }
  return row;
}

const mounted: Array<{ root: ReturnType<typeof createRoot>; container: HTMLDivElement; queryClient: QueryClient }> = [];

function actionResponse(path: string, value: unknown): unknown {
  if (value instanceof Response || value === undefined || value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) {
    if (path.endsWith("/action-groups")) {
      if (!value.every(isActionGroup)) throw new Error("Expected ActionGroup fixtures.");
      return listActionGroupsProtoJson(value);
    }
    if (path.endsWith("/action-requests")) {
      if (!value.every(isActionRequest)) throw new Error("Expected ActionRequest fixtures.");
      return listActionRequestsProtoJson(value);
    }
    return value;
  }
  if (!isRecord(value)) return value;
  if (path.endsWith("/action-requests:history")) {
    if (!isListActionRequestHistoryResponse(value))
      throw new Error("Expected an ListActionRequestHistoryResponse fixture.");
    return listActionHistoryProtoJson(value);
  }
  if (path.endsWith("/decision")) {
    if (!isActionRequest(value)) throw new Error("Expected an ActionRequest decision fixture.");
    return decideActionRequestProtoJson(value);
  }
  if (path.endsWith("/action-groups")) {
    return value;
  }
  if (/\/action-requests\/[^/]+$/.test(path)) {
    if (!isActionRequest(value)) throw new Error("Expected an ActionRequest fixture.");
    return getActionRequestProtoJson(value);
  }
  if (path.endsWith("/linkage/start")) {
    if (typeof value.authorization_url !== "string") throw new Error("Expected an authorization URL fixture.");
    return startMcpLinkageProtoJson(value.authorization_url);
  }
  if (path.endsWith("/linkage/disconnect")) {
    if (!isMcpServerLinkage(value.linkage)) throw new Error("Expected an MCP linkage fixture.");
    return disconnectMcpLinkageProtoJson(value.linkage);
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isActionRequest(value: unknown): value is ActionRequest {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.idempotencyKey === "string" &&
    isRecord(value.action) &&
    typeof value.state === "number" &&
    typeof value.version === "bigint" &&
    isRecord(value.createdAt) &&
    isRecord(value.updatedAt)
  );
}

function isActionGroup(value: unknown): value is ActionGroup {
  return (
    isRecord(value) &&
    typeof value.key === "string" &&
    typeof value.executorKind === "number" &&
    Array.isArray(value.actions)
  );
}

function isMcpServerLinkage(value: unknown): value is McpServerLinkage {
  return (
    isRecord(value) &&
    typeof value.serverId === "string" &&
    typeof value.serverUrl === "string" &&
    typeof value.status === "number" &&
    Array.isArray(value.scopes)
  );
}

function isListActionRequestHistoryResponse(
  value: Record<string, unknown>
): value is Record<string, unknown> & ListActionRequestHistoryResponse {
  return (
    Array.isArray(value.actionRequests) &&
    value.actionRequests.every(isActionRequest) &&
    (value.nextCursor === undefined || typeof value.nextCursor === "string")
  );
}

type ActionFixture = ActionRequest | ActionGroup | McpServerLinkage;

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

  sendActionRequests(value: readonly ActionRequest[]): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(
      this.encoder.encode(
        `${JSON.stringify({ result: actionRequestsSnapshotProtoJson(value) })}\n${JSON.stringify({ result: actionRequestsSnapshotCompleteProtoJson() })}\n`
      )
    );
  }

  sendActionGroups(value: readonly ActionGroup[]): void {
    if (this.controller === undefined) return;
    this.controller.enqueue(
      this.encoder.encode(
        `${JSON.stringify({ result: actionGroupsSnapshotProtoJson(value) })}\n${JSON.stringify({ result: actionGroupsSnapshotCompleteProtoJson() })}\n`
      )
    );
  }

  sendMcpLinkages(value: readonly McpServerLinkage[]): void {
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
  const queryClient = createAppQueryClient();
  mounted.push({ root, container, queryClient });
  await act(async () => {
    root.render(
      <QueryClientProvider client={queryClient}>
        <MantineProvider env="test">{node}</MantineProvider>
      </QueryClientProvider>
    );
  });
  return container;
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
  if (watch === undefined) {
    watchUrl ??= url === undefined ? ACTION_REQUEST_WATCH : undefined;
    if (watchUrl !== undefined) watch = await TestNdjsonWatch.waitForOpen(watchUrl);
  }
  if (watch !== undefined && typeof value !== "string") {
    await act(async () => {
      if (watchUrl === ACTION_GROUP_WATCH) {
        const groups = value.map((item) => {
          if (!isActionGroup(item)) throw new Error("Expected ActionGroup fixtures.");
          return item;
        });
        watch.sendActionGroups(groups);
      } else if (watchUrl === MCP_LINKAGE_WATCH) {
        const linkages = value.map((item) => {
          if (!isMcpServerLinkage(item)) throw new Error("Expected McpServerLinkage fixtures.");
          return item;
        });
        watch.sendMcpLinkages(linkages);
      } else {
        const requests = value.map((item) => {
          if (!isActionRequest(item)) throw new Error("Expected ActionRequest fixtures.");
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
  throw new Error(`No watch is open for ${url ?? "the Action requests"}`);
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
        if (!isActionGroup(value)) throw new Error("Expected ActionGroup upserts.");
        return actionGroupUpsertProtoJson(value);
      });
      watch.sendGroupUpsertFrames(groups);
      return;
    }
    if (watchUrl === MCP_LINKAGE_WATCH) {
      const linkages = values.map((value) => {
        if (!isMcpServerLinkage(value)) throw new Error("Expected McpServerLinkage upserts.");
        return mcpLinkageUpsertProtoJson(value);
      });
      watch.sendLinkageUpsertFrames(linkages);
      return;
    }
    const requests = values.map((value) => {
      if (!isActionRequest(value)) throw new Error("Expected ActionRequest upserts.");
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
export async function renderLive(node: ReactNode, pending: readonly ActionRequest[] = []): Promise<HTMLDivElement> {
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
  item.queryClient.clear();
  item.container.remove();
}

afterEach(async () => {
  for (const item of mounted.splice(0)) {
    await act(async () => item.root.unmount());
    item.queryClient.clear();
    item.container.remove();
  }
  for (const eject of ejects.splice(0)) eject();
  for (const watch of TestNdjsonWatch.opened.values()) watch.close();
  TestNdjsonWatch.reset();
  actionAnswer = () => undefined;
  actionFetchInstalled = false;
  vi.unstubAllGlobals();
});
