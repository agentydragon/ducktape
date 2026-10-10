import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it } from "vitest";

type PushMessage =
  | { kind: "show"; action_id: string; action_group: string; action_name: string; version?: number }
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
  setCurrent(action: { state: string; version: number } | null): void;
};

function loadWorker(): TestWorker {
  const listeners: TestWorker["listeners"] = new Map();
  const requests: TestWorker["requests"] = [];
  const windows: TestWorker["windows"] = [];
  const notices: TestWorker["notices"] = [];
  let current: { state: string; version: number } | null = { state: "decision_pending", version: 1 };

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
};

async function show(worker: TestWorker, message = showMessage): Promise<Notice> {
  await dispatch(worker, "push", { data: { json: () => message } });
  const notice = worker.notices.at(-1);
  if (!notice) throw new Error("service worker did not show a notification");
  return notice;
}

describe("service worker", () => {
  it("keeps the existing notification text and buttons", async () => {
    const worker = loadWorker();

    const notice = await show(worker);

    expect({ title: notice.title, body: notice.body }).toMatchInlineSnapshot(`
      {
        "body": "Action requires approval",
        "title": "finance / transfer",
      }
    `);
    expect(notice.actions).toEqual([
      { action: "approve", title: "Approve" },
      { action: "deny", title: "Deny" },
    ]);
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

    worker.setCurrent({ state: "denied", version: 2 });
    expect((await show(worker)).actions).toBeUndefined();

    worker.setCurrent(null);
    const unavailable = await show(worker);
    expect(unavailable.actions).toEqual([]);
    expect({ title: unavailable.title, body: unavailable.body }).toMatchInlineSnapshot(`
      {
        "body": "Open Agentplane to review this Action",
        "title": "finance / transfer",
      }
    `);
  });
});
