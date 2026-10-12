import { MantineProvider } from "@mantine/core";
import { type ReactNode, act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, type Mock, vi } from "vitest";

import { api } from "../client";
import type { ActionRequestView, ActionState } from "./client";

/** Shared fixtures, stand-ins for the network, and mounting for the Action views' suites. */

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

export const DECISION_NOTE = "test decision note";

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

/** Mounts `node` in the app's Mantine provider, until the test ends. */
export async function mount(node: ReactNode): Promise<HTMLDivElement> {
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
  vi.unstubAllGlobals();
});
