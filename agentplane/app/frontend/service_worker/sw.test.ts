import { readFileSync } from "node:fs";
import { TextDecoder, TextEncoder } from "node:util";
import { URLSearchParams } from "node:url";
import { runInNewContext } from "node:vm";
import { fromJson } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";

import { Verdict as ProtoVerdict } from "../../../action_service/action_service_pb";
import { DecideActionRequestRequestSchema } from "../../../action_service/operator_action_service_pb";
import type { ActionRequestView } from "../actions/types";
import { decideActionRequestProtoJson, listActionRequestsProtoJson } from "../actions/testing/proto_fixtures";

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
  closed: string[];
  setPending(actions: ActionRequestView[] | null): void;
};

function actionRequest(
  fields: Pick<ActionRequestView, "action" | "arguments" | "title" | "description">
): ActionRequestView {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    idempotency_key: "notification-format-test",
    ...fields,
    caller: { namespace: "test-namespace", name: "test-account" },
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
  const closed: string[] = [];
  let pending: ActionRequestView[] | null = [
    actionRequest({
      action: { group: "finance", name: "transfer" },
      title: "Transfer $400 to vendor",
      description: "Invoice 2026-04-18.",
      arguments: { recipient: "vendor", amount: 400, account: "checking" },
    }),
  ];

  const self = {
    addEventListener: (
      kind: string,
      handler: (event: TestEvent & { waitUntil(promise: Promise<unknown>): void }) => void
    ) => listeners.set(kind, handler),
    registration: {
      showNotification: async (title: string, options: Omit<Notice, "title">) => {
        notices.push({ title, ...options });
      },
      getNotifications: async (filter: { tag: string }) =>
        notices
          .filter((notice) => notice.tag === filter.tag)
          .map((notice) => ({ close: () => closed.push(notice.tag ?? "") })),
    },
    clients: { openWindow: async (url: string) => windows.push(url), claim: async () => {} },
    skipWaiting: async () => {},
  };

  runInNewContext(readFileSync(new URL("./bundled/sw.js", import.meta.url), "utf8"), {
    self,
    TextDecoder,
    TextEncoder,
    URLSearchParams,
    fetch: async (url: string, options?: RequestInit) => {
      requests.push({ url, options });
      const actionRequest = pending?.[0];
      const body =
        options?.method === "POST"
          ? actionRequest === undefined
            ? {}
            : decideActionRequestProtoJson(actionRequest)
          : listActionRequestsProtoJson(pending ?? []);
      return { ok: options?.method === "POST" || pending !== null, json: async () => body };
    },
    crypto: { randomUUID: () => "test-intent" },
  });

  return { listeners, requests, windows, notices, closed, setPending: (actions) => (pending = actions) };
}

async function dispatch(worker: TestWorker, kind: string, event: TestEvent): Promise<void> {
  const handler = worker.listeners.get(kind);
  if (!handler) throw new Error(`service worker did not register ${kind}`);

  let work: Promise<unknown> | undefined;
  handler({
    ...event,
    waitUntil: (promise) => {
      work = promise;
    },
  });
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
    worker.setPending([
      actionRequest({
        action: { group: "ssh", name: "exec" },
        title: "Check disk space",
        description: "Run the disk usage check on the build host.",
        arguments: { user: "deploy", host: "build-01", command: "df -h", timeout_seconds: 30 },
      }),
    ]);

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
    const approved = fromJson(DecideActionRequestRequestSchema, JSON.parse(String(worker.requests[0]?.options?.body)));
    expect(approved.verdict).toBe(ProtoVerdict.ALLOW);
    expect(approved.expectedVersion).toBe(1n);
    expect(worker.requests[0]?.url).toBe("/upstream/actions/v1/operator/action-requests/request/decision");
    expect(approved.idempotencyKey).toEqual(expect.any(String));
    expect(approved.decisionNote).toBeUndefined();
    expect(worker.requests[0]?.options?.credentials).toBeUndefined();

    await dispatch(worker, "notificationclick", { action: "deny", notification });
    const denied = fromJson(DecideActionRequestRequestSchema, JSON.parse(String(worker.requests[1]?.options?.body)));
    expect(denied.verdict).toBe(ProtoVerdict.DENY);
    expect(denied.expectedVersion).toBe(1n);
    expect(worker.requests[1]?.url).toBe("/upstream/actions/v1/operator/action-requests/request/decision");
    expect(denied.idempotencyKey).toEqual(expect.any(String));
    expect(worker.windows).toEqual(["/#/actions"]);

    const retracted = await show(worker, { kind: "retract", action_id: "request", outcome: "denied" });
    expect(retracted.silent).toBe(true);
  });

  it("keeps one notification for any number of waiting Actions", async () => {
    const worker = loadWorker();
    const first = actionRequest({
      action: { group: "finance", name: "transfer" },
      title: "First",
      description: null,
      arguments: {},
    });
    const second = { ...first, id: "00000000-0000-4000-8000-000000000002", title: "Second" };
    worker.setPending([first, second]);

    const notice = await show(worker);
    await show(worker);

    expect(new Set(worker.notices.map((each) => each.tag)).size).toBe(1);
    expect({ title: notice.title, body: notice.body, actions: notice.actions }).toMatchInlineSnapshot(`
      {
        "actions": undefined,
        "body": "First · finance / transfer
      Second · finance / transfer",
        "title": "2 Actions waiting",
      }
    `);
    expect(notice.requireInteraction).toBe(true);
  });

  it("clears the notification once nothing waits, and degrades without session authority", async () => {
    const worker = loadWorker();
    worker.setPending([]);
    await show(worker, { kind: "retract", action_id: "request", outcome: "denied" });
    expect(worker.closed).toEqual(["agentplane-pending"]);

    worker.setPending(null);
    expect((await show(worker)).actions).toBeUndefined();
  });
});
