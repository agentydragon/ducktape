import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

import type { ActionRequestView } from "../actions/types";

type PushMessage =
  | {
      kind: "show";
      action_id: string;
      action_group: string;
      action_name: string;
      version: number;
      url: string;
    }
  | { kind: "retract"; action_id: string; outcome: string };

type Notice = {
  title: string;
  body?: string;
  tag?: string;
  actions?: Array<{ action: string; title: string }>;
  data?: unknown;
  silent?: boolean;
  requireInteraction?: boolean;
};

type TestEvent = {
  action?: string;
  data?: { json(): unknown } | null;
  notification?: { data: unknown; close(): void };
};

type TestWorker = {
  listeners: Map<string, (event: TestEvent & { waitUntil(promise: Promise<unknown>): void }) => void>;
  requests: Array<{ url: string; options?: RequestInit }>;
  windows: string[];
  notices: Notice[];
  setCurrent(action: ActionRequestView | null): void;
};

function actionRequest(
  fields: Pick<ActionRequestView, "action" | "arguments" | "title" | "description">
): ActionRequestView {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    idempotency_key: "notification-format-test",
    ...fields,
    caller: null,
    external_grant: null,
    state: "decision_pending",
    version: 1,
    created_at: "2026-10-09T12:00:00Z",
    updated_at: "2026-10-09T12:00:00Z",
    decision: null,
    execution: null,
  };
}

function loadWorker(): TestWorker {
  const listeners: TestWorker["listeners"] = new Map();
  const requests: TestWorker["requests"] = [];
  const windows: TestWorker["windows"] = [];
  const notices: TestWorker["notices"] = [];
  let current: ActionRequestView | null = actionRequest({
    action: { group: "finance", name: "transfer" },
    title: "Transfer $400 to vendor",
    description: "Invoice 2026-04-18.",
    arguments: { recipient: "vendor", amount: 400, account: "checking" },
  });

  const self = {
    addEventListener: (
      kind: string,
      handler: (event: TestEvent & { waitUntil(promise: Promise<unknown>): void }) => void
    ) => listeners.set(kind, handler),
    registration: {
      showNotification: async (title: string, options: Omit<Notice, "title">) => {
        notices.push({ title, ...options });
      },
    },
    clients: { openWindow: async (url: string) => windows.push(url), claim: async () => {} },
    skipWaiting: async () => {},
  };

  runInNewContext(readFileSync(new URL("./bundled/sw.js", import.meta.url), "utf8"), {
    self,
    fetch: async (url: string, options?: RequestInit) => {
      requests.push({ url, options });
      return { ok: current !== null, json: async () => current };
    },
    crypto: { randomUUID: () => "test-intent" },
  });

  return { listeners, requests, windows, notices, setCurrent: (action) => (current = action) };
}

async function dispatch(worker: TestWorker, kind: string, event: TestEvent): Promise<void> {
  const handler = worker.listeners.get(kind);
  if (!handler) throw new Error(`service worker did not register ${kind}`);

  let work: Promise<unknown> | undefined;
  handler({ ...event, waitUntil: (promise) => (work = promise) });
  await work;
}

const showMessage: PushMessage = {
  kind: "show",
  action_id: "request",
  action_group: "finance",
  action_name: "transfer",
  version: 1,
  url: "/#/actions",
};

async function show(worker: TestWorker, message = showMessage): Promise<Notice> {
  await dispatch(worker, "push", { data: { json: () => message } });
  const notice = worker.notices.at(-1);
  if (!notice) throw new Error("service worker did not show a notification");
  return notice;
}

describe("service worker notifications", () => {
  it("renders the generic Action title and body", async () => {
    const worker = loadWorker();

    const notice = await show(worker);

    expect({ title: notice.title, body: notice.body }).toMatchInlineSnapshot(`
      {
        "body": "Invoice 2026-04-18.",
        "title": "Transfer $400 to vendor · finance / transfer",
      }
    `);
    expect(notice.actions).toHaveLength(2);
  });

  it("renders SSH exec with its target, command, and timeout", async () => {
    const worker = loadWorker();
    worker.setCurrent(
      actionRequest({
        action: { group: "ssh", name: "exec" },
        title: "Check disk space",
        description: "Run the disk usage check on the build host.",
        arguments: { user: "deploy", host: "build-01", command: "df -h", timeout_seconds: 30 },
      })
    );

    const notice = await show(worker, {
      ...showMessage,
      action_group: "ssh",
      action_name: "exec",
    });

    expect({ title: notice.title, body: notice.body }).toMatchInlineSnapshot(`
      {
        "body": "$ df -h · Timeout 30 s",
        "title": "Check disk space · Run command on deploy@build-01",
      }
    `);
  });

  it("only decides on explicit buttons and removes stale decision buttons", async () => {
    const worker = loadWorker();
    const notification = { data: showMessage, close: () => {} };

    await dispatch(worker, "notificationclick", { action: "", notification });
    expect(worker.requests).toHaveLength(0);
    expect(worker.windows).toEqual(["/#/actions"]);

    await dispatch(worker, "notificationclick", { action: "approve", notification });
    expect(JSON.parse(String(worker.requests[0]?.options?.body))).toMatchObject({
      verdict: "allow",
      expected_version: 1,
    });
    expect(worker.requests[0]?.options?.credentials).toBe("include");

    await dispatch(worker, "notificationclick", { action: "deny", notification });
    expect(JSON.parse(String(worker.requests[1]?.options?.body))).toMatchObject({ verdict: "deny" });

    const retracted = await show(worker, { kind: "retract", action_id: "request", outcome: "denied" });
    expect(retracted.tag).toBe("request");
    expect(retracted.actions).toBeUndefined();

    worker.setCurrent({
      ...actionRequest({
        action: { group: "ssh", name: "exec" },
        title: "Check disk space",
        description: "Run the disk usage check on the build host.",
        arguments: { user: "deploy", host: "build-01", command: "df -h", timeout_seconds: 30 },
      }),
      state: "denied",
      version: 2,
    });
    expect((await show(worker)).actions).toBeUndefined();

    worker.setCurrent(null);
    expect((await show(worker)).actions).toEqual([]);
  });
});
