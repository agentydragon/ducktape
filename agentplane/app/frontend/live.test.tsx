// @vitest-environment happy-dom
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { SandboxView } from "./client";
import { zDecisionsSnapshot } from "./api/live_schema.zod";
import { liveSchemas, useLive } from "./live";
import { parseJsonFrame } from "./live_stream";

class TestEventSource extends EventTarget {
  static readonly CLOSED = 2;
  readyState = 0;

  constructor(readonly url: string) {
    super();
    sources.push(this);
  }

  close(): void {
    this.readyState = TestEventSource.CLOSED;
  }

  frame(name: string, data: string): void {
    this.readyState = 1;
    this.dispatchEvent(new MessageEvent(name, { data }));
  }
}

let sources: TestEventSource[] = [];
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;

const watch = {
  fresh: true,
  stale_after_seconds: 60,
  refreshed_seconds_ago: { sandboxes: 1.25 },
};

const sandbox: SandboxView = {
  name: "live-sandbox",
  uid: "00000000-0000-4000-8000-000000000001",
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00+00:00",
  operating_mode: "Running",
  service_account: { namespace: "agentplane-test", name: "live-sandbox" },
  status: { conditions: [{ type: "Ready", status: "True" }] },
  phase: "Running",
  pod: null,
  wake_on_send: false,
};

beforeEach(async () => {
  sources = [];
  vi.stubGlobal("EventSource", TestEventSource);
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<LiveProbe />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function LiveProbe(): JSX.Element {
  const live = useLive("/live/sandboxes", "Sandboxes", liveSchemas.sandboxes);
  return <output>{JSON.stringify(live)}</output>;
}

function state(): Record<string, unknown> {
  const output = container.querySelector("output");
  if (!output) throw new Error("live probe has not rendered");
  return JSON.parse(output.textContent ?? "{}") as Record<string, unknown>;
}

function stream(): TestEventSource {
  const current = sources.at(-1);
  if (!current) throw new Error("live stream has not opened");
  return current;
}

it("accepts producer-shaped generated schemas, nested snapshots, defaults, nulls, and UTC timestamps", () => {
  const sandboxes = parseJsonFrame(
    liveSchemas.sandboxes.snapshot,
    JSON.stringify({ sandboxes: [sandbox], watch }),
    "Snapshot"
  );
  expect(sandboxes.success).toBe(true);
  if (!sandboxes.success) throw new Error(sandboxes.error);
  expect(sandboxes.data.sandboxes[0]?.pod).toBeNull();
  expect(sandboxes.data.sandboxes[0]?.status?.conditions[0]?.status).toBe("True");

  const threads = parseJsonFrame(
    liveSchemas.threads.snapshot,
    JSON.stringify({
      sandboxes: [sandbox],
      watch,
      threads: [],
      more: false,
      archived_count: 0,
      updates_connected: true,
    }),
    "Snapshot"
  );
  expect(threads.success).toBe(true);

  const sandboxDetail = parseJsonFrame(
    liveSchemas.sandbox.snapshot,
    JSON.stringify({
      sandbox,
      bindings: [],
      action_policy: null,
      threads: { kind: "unavailable", code: "UNAVAILABLE" },
      updates_connected: true,
      watch,
    }),
    "Snapshot"
  );
  expect(sandboxDetail.success).toBe(true);

  const decisions = {
    watching: true,
    decisions: [
      {
        at: "2026-01-01T01:02:03+00:00",
        method: "GET",
        host: "packages.example.test",
        port: 443,
        outcome: "deny",
        reason: "host_not_allowed",
        binding: null,
        policy: null,
        rule: null,
        substituted: false,
        address: null,
      },
      {
        at: "2026-01-01T01:02:04Z",
        method: "POST",
        host: "api.example.test",
        port: 443,
        outcome: "allow",
        reason: null,
        binding: "packages",
        policy: "default-egress",
        rule: 2,
        substituted: true,
        address: "192.0.2.10",
      },
    ],
  };
  const egress = parseJsonFrame(zDecisionsSnapshot, JSON.stringify(decisions), "Egress snapshot");
  expect(egress.success).toBe(true);
  if (egress.success) {
    expect(egress.data.decisions).toHaveLength(2);
    expect(egress.data.decisions[0]).toMatchObject({ reason: "host_not_allowed", substituted: false, address: null });
  }
});

it.each(["{", JSON.stringify({ sandboxes: [{ name: "partial" }], watch })])(
  "rejects malformed or structurally invalid frames without losing the last snapshot (%s)",
  async (invalidFrame) => {
    const good = JSON.stringify({ sandboxes: [sandbox], watch });
    await act(async () => stream().frame("snapshot", good));
    expect(state().snapshot).toMatchObject({ sandboxes: [{ name: "live-sandbox" }] });
    expect(state().stream).toMatchObject({ connection: { phase: "live" }, standing: "current" });

    await act(async () => stream().frame("snapshot", invalidFrame));
    expect(state().snapshot).toMatchObject({ sandboxes: [{ name: "live-sandbox" }] });
    expect((state().stream as { stalled: string }).stalled).toEqual(expect.stringMatching(/^Invalid Snapshot frame/));
    expect(state().stream).toMatchObject({ connection: { phase: "invalid" }, standing: "stale" });

    await act(async () => stream().frame("health", JSON.stringify(watch)));
    expect(state().stream).toMatchObject({ connection: { phase: "live" }, standing: "stale" });
    expect((state().stream as { stalled: string }).stalled).toEqual(expect.stringMatching(/^Invalid Snapshot frame/));

    await act(async () => stream().frame("snapshot", good));
    expect((state().stream as { stalled: null }).stalled).toBeNull();
    expect(state().stream).toMatchObject({ connection: { phase: "live" }, standing: "current" });
  }
);
