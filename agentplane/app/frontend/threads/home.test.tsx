// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "../client";
import { Home } from "./home";
import { RoutedThreadsLiveProvider, type ThreadsSnapshot } from "../live";
import type { SessionNoticeStates } from "../notification_types";
import { Sidebar } from "../sidebar";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const SANDBOX: SandboxView = {
  name: "test-sandbox",
  uid: "00000000-0000-4000-8000-000000000001",
  namespace: "agentplane-test",
  created_at: "2026-01-01T00:00:00Z",
  operating_mode: "Running",
  service_account: { namespace: "agentplane-test", name: "test-sandbox" },
  status: { conditions: [{ type: "Ready", status: "True" }] },
  phase: "Running",
  wake_on_send: false,
};

function thread(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "name">): ThreadView {
  return {
    sandbox: SANDBOX.name,
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    archived: false,
    last_cursor: 0,
    harness_state: "HARNESS_STATE_RUNNING",
    feed_status: "active",
    ...overrides,
  };
}

const THREADS = [
  thread({ id: "00000000-0000-4000-8000-000000000001", name: "Alpha thread", last_event_at: "2026-01-01T11:58:00Z" }),
  thread({ id: "00000000-0000-4000-8000-000000000002", name: "Beta thread" }),
];

/** Renders Home beside the Sidebar, the way the shell lays them out on desktop; returns the URL of
 * every threads stream opened, in order. */
async function render(): Promise<string[]> {
  const threadStreams: string[] = [];
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      readyState = 0;
      constructor(url: string) {
        super();
        const { pathname, searchParams } = new URL(url, "http://test");
        if (pathname === "/threads/notices/stream") {
          const states: SessionNoticeStates = { observedAt: "2026-01-01T00:00:00Z", inboxes: [] };
          queueMicrotask(() => this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(states) })));
          return;
        }
        expect(pathname).toBe("/live/threads");
        threadStreams.push(url);
        const search = searchParams.get("search")?.toLowerCase();
        const snapshot: ThreadsSnapshot = {
          threads: THREADS.filter(
            (candidate) => search === undefined || candidate.name?.toLowerCase().includes(search)
          ),
          more: false,
          archived_count: 0,
          sandboxes: [SANDBOX],
          updates_connected: true,
          watch: { fresh: true, stale_after_seconds: 90, refreshed_seconds_ago: { sandboxes: 0 } },
        };
        queueMicrotask(() => this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot) })));
      }
      close(): void {}
    }
  );
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <RoutedThreadsLiveProvider>
            <Sidebar settingsOpen={false} onOpenSettings={() => undefined} open onClose={() => undefined} />
            <Home />
          </RoutedThreadsLiveProvider>
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return threadStreams;
}

function home(): HTMLElement {
  const found = container.querySelector<HTMLElement>('section[aria-label="Sandboxes and threads"]');
  if (!found) throw new Error("missing Home");
  return found;
}

function sidebar(): HTMLElement {
  const found = container.querySelector<HTMLElement>("nav.agentplane-sidebar");
  if (!found) throw new Error("missing sidebar");
  return found;
}

function row(scope: HTMLElement, name: string): HTMLElement {
  const found = [...scope.querySelectorAll<HTMLElement>(".agentplane-thread-list-row")].find((candidate) =>
    candidate.textContent?.includes(name)
  );
  if (!found) throw new Error(`missing row ${name}`);
  return found;
}

function searchBox(scope: HTMLElement): HTMLInputElement {
  const found = scope.querySelector('input[aria-label="Search threads"]');
  if (!(found instanceof HTMLInputElement)) throw new Error("missing search box");
  return found;
}

it("adds each Thread's harness, model and last activity to its row, which the sidebar leaves out", async () => {
  await render();
  const alpha = row(home(), "Alpha thread").querySelector(".agentplane-thread-list-row-detail");
  expect(alpha?.textContent).toContain("Claude · test-model");
  expect(alpha?.querySelector("time")?.dateTime).toBe("2026-01-01T11:58:00.000Z");
  expect(row(home(), "Beta thread").querySelector(".agentplane-thread-list-row-detail time")).toBeNull();
  expect(sidebar().querySelector(".agentplane-thread-list-row-detail")).toBeNull();
});

it("shares one search with the sidebar, as one threads stream", async () => {
  const threadStreams = await render();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const input = searchBox(home());
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "beta");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(searchBox(sidebar()).value).toBe("beta");
  await act(async () => vi.runAllTimers());
  expect(threadStreams.map((url) => new URL(url, "http://test").searchParams.get("search"))).toEqual([null, "beta"]);
  for (const scope of [home(), sidebar()]) {
    expect(row(scope, "Beta thread")).toBeDefined();
    expect(scope.textContent).not.toContain("Alpha thread");
  }
});
