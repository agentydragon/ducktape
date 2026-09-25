// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { type JSX, act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import type { ThreadsSnapshot } from "./live";
import { Sidebar } from "./sidebar";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
beforeEach(() => vi.stubGlobal("fetch", fetchMock));
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

function sandbox(name: string, overrides: Partial<SandboxView> = {}): SandboxView {
  return {
    name,
    uid: `00000000-0000-4000-8000-${name.padStart(12, "0").slice(-12)}`,
    state: "running",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    service_account: { namespace: "agentplane-test", name },
    conditions: [],
    ...overrides,
  };
}

function thread(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "sandbox" | "session_id">): ThreadView {
  return {
    harness: "HARNESS_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_cursor: 0,
    harness_state: "HARNESS_STATE_UNSPECIFIED",
    ...overrides,
  };
}

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

function snapshot(threads: ThreadView[], sandboxes: Record<string, SandboxView>): ThreadsSnapshot {
  return {
    threads,
    sandboxes: Object.values(sandboxes),
    updates_connected: true,
    watch: { fresh: true, stale_after_seconds: 90, refreshed_seconds_ago: { sandboxes: 0 } },
  };
}

async function pushSnapshot(stream: EventTarget, value: ThreadsSnapshot): Promise<void> {
  await act(async () => stream.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(value) })));
}

async function render(
  threads: ThreadView[],
  sandboxes: Record<string, SandboxView>,
  options: { initialPath?: string; settingsOpen?: boolean; mobileOpen?: boolean } = {}
): Promise<{ onOpenSettings: ReturnType<typeof vi.fn>; onMobileClose: ReturnType<typeof vi.fn>; stream: EventTarget }> {
  const streams: EventTarget[] = [];
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      // An error is the network's, which the browser retries: the source stays CONNECTING.
      readyState = 0;
      constructor(url: string) {
        super();
        expect(url).toBe("/live/threads");
        streams.push(this);
        queueMicrotask(() =>
          this.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(snapshot(threads, sandboxes)) }))
        );
      }
      close(): void {}
    }
  );
  fetchMock.mockImplementation((request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/models") {
      return Promise.resolve(Response.json({ HARNESS_CLAUDE: [], HARNESS_CODEX: [] }));
    }
    if (/^\/threads\/[^/]+\/(un)?archive$/.test(url.pathname)) {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    throw new Error(`Unexpected request: ${request.method} ${url.pathname}`);
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const onOpenSettings = vi.fn();
  const onMobileClose = vi.fn();
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[options.initialPath ?? "/"]}>
          <Sidebar
            settingsOpen={options.settingsOpen ?? false}
            onOpenSettings={onOpenSettings}
            mobileOpen={options.mobileOpen ?? false}
            onMobileClose={onMobileClose}
          />
          <Routes>
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return { onOpenSettings, onMobileClose, stream: streams[0] };
}

function rows(): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[role="button"]')];
}

function row(name: string): HTMLElement {
  const found = rows().find((candidate) => candidate.textContent?.includes(name));
  if (!found) throw new Error(`missing row ${name}`);
  return found;
}

function location(): string | null | undefined {
  return container.querySelector('[data-testid="location"]')?.textContent;
}

it("applies pushed renames and Sandbox state without marking a suspended harness live", async () => {
  const running = thread({
    id: "t-1",
    sandbox: "test-sandbox",
    session_id: "s-1",
    name: "Before rename",
    harness_state: "HARNESS_STATE_RUNNING",
  });
  const sandboxes = { "test-sandbox": sandbox("test-sandbox") };
  const { stream } = await render([running], sandboxes, { initialPath: "/threads/t-1" });
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(1);

  const renamed = { ...running, name: "Renamed in another replica" };
  await pushSnapshot(
    stream,
    snapshot([renamed], {
      "test-sandbox": sandbox("test-sandbox", { state: "suspended", operating_mode: "Suspended" }),
      "test-threadless": sandbox("test-threadless", { state: "waiting_for_pod" }),
    })
  );
  expect(container.textContent).not.toContain("Before rename");
  expect(row(renamed.name).className).toContain("current");
  expect(container.querySelector(".agentplane-sidebar-state-icon.suspended")).not.toBeNull();
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(0);
  expect(container.textContent).toContain("test-threadless");
  expect(container.textContent).toContain("0 threads");
  expect(fetchMock).not.toHaveBeenCalled();

  await pushSnapshot(stream, snapshot([renamed], {}));
  expect(container.querySelector('a[href="/sandboxes/test-sandbox"]')).toBeNull();
  await act(async () => row(renamed.name).click());
  expect(location()).toBe("/threads/t-1");
});

it("keeps retained rows but withdraws live indicators when any update source is unavailable", async () => {
  const threads = [
    thread({
      id: "t-1",
      sandbox: "test-sandbox",
      session_id: "s-1",
      name: "Retained thread",
      harness_state: "HARNESS_STATE_RUNNING",
    }),
  ];
  const sandboxes = { "test-sandbox": sandbox("test-sandbox") };
  const { stream } = await render(threads, sandboxes);
  await act(async () => stream.dispatchEvent(new Event("error")));
  expect(container.textContent).toContain("Not connected to the live stream");
  expect(container.textContent).toContain("Retained thread");
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(0);

  await pushSnapshot(stream, { ...snapshot(threads, sandboxes), updates_connected: false });
  expect(container.textContent).not.toContain("Not connected to the live stream");
  expect(container.textContent).toContain("Thread updates disconnected");
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(0);

  const stale = snapshot(threads, sandboxes);
  stale.watch.fresh = false;
  await pushSnapshot(stream, stale);
  expect(container.textContent).toContain("watch has stopped moving");
  expect(container.textContent).not.toContain("Thread updates disconnected");
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(0);

  await pushSnapshot(stream, snapshot([{ ...threads[0], name: "Current thread" }], sandboxes));
  expect(container.textContent).not.toContain("Retained thread");
  expect(container.textContent).not.toContain("watch has stopped moving");
  expect(container.querySelectorAll(".agentplane-sidebar-dot.ok")).toHaveLength(1);
});

it("groups threads by sandbox, showing each group's state, name and visible thread count", async () => {
  await render(
    [
      thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "List the repository files" }),
      thread({ id: "t-2", sandbox: "demo-a1b2", session_id: "s-2", name: "Clean up the stale branch" }),
      thread({ id: "t-3", sandbox: "prod-x7f2", session_id: "s-3", name: "Investigate flaky CI" }),
    ],
    { "demo-a1b2": sandbox("demo-a1b2"), "prod-x7f2": sandbox("prod-x7f2", { state: "suspended" }) }
  );

  expect(container.textContent).toContain("demo-a1b2");
  expect(container.textContent).toContain("2 threads");
  expect(container.textContent).toContain("prod-x7f2");
  expect(container.textContent).toContain("1 thread");
  expect(container.textContent).toContain("List the repository files");
  expect(container.textContent).toContain("Investigate flaky CI");
});

it("opens retained Thread history even when its Sandbox is gone", async () => {
  await render(
    [thread({ id: "t-1", sandbox: "old-debug-3f9c", session_id: "s-9", name: "Why did the migration hang" })],
    {}
  );

  const label = [...container.querySelectorAll(".agentplane-sidebar-group-name")].find(
    (node) => node.textContent === "old-debug-3f9c"
  );
  expect(label).toBeDefined();
  expect((label as HTMLElement).style.textDecoration).toContain("line-through");
  expect(label?.tagName).toBe("SPAN");
  expect(container.querySelector('a[href="/sandboxes/old-debug-3f9c"]')).toBeNull();

  const readonlyRow = [...container.querySelectorAll(".agentplane-sidebar-row.readonly")].find((node) =>
    node.textContent?.includes("Why did the migration hang")
  );
  expect(readonlyRow).toBeDefined();
  expect(readonlyRow?.getAttribute("role")).toBe("button");
  await act(async () => (readonlyRow as HTMLElement).click());
  expect(location()).toBe("/threads/t-1");
});

it("opens the details of a provisioning Sandbox with no Threads", async () => {
  const pending = sandbox("test-provisioning", { state: "waiting_for_pod" });
  await render([], { [pending.name]: pending });
  expect(container.textContent).toContain("0 threads");
  expect(container.querySelector(".agentplane-sidebar-state-icon.pending")).not.toBeNull();
  const link = container.querySelector('a[href="/sandboxes/test-provisioning"]');
  if (!(link instanceof HTMLAnchorElement)) throw new Error("missing provisioning Sandbox link");
  await act(async () => link.click());
  expect(location()).toBe("/sandboxes/test-provisioning");
});

it.each([false, true])(
  "links a Sandbox name to its details without changing Thread navigation (mobile=%s)",
  async (mobileOpen) => {
    const { onMobileClose } = await render(
      [thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "First thread" })],
      { "demo-a1b2": sandbox("demo-a1b2") },
      { initialPath: "/threads/t-1", mobileOpen }
    );
    const link = container.querySelector('a[href="/sandboxes/demo-a1b2"]');
    if (!(link instanceof HTMLAnchorElement)) throw new Error("missing Sandbox details link");
    expect(link.textContent).toBe("demo-a1b2");

    await act(async () => link.click());
    expect(location()).toBe("/sandboxes/demo-a1b2");
    expect(onMobileClose).toHaveBeenCalledOnce();

    await act(async () => row("First thread").click());
    expect(location()).toBe("/threads/t-1");
  }
);

it("opens the stable Thread route and highlights that Thread", async () => {
  await render(
    [
      thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "First thread" }),
      thread({ id: "t-2", sandbox: "demo-a1b2", session_id: "s-2", name: "Second thread" }),
    ],
    { "demo-a1b2": sandbox("demo-a1b2") },
    { initialPath: "/threads/t-2" }
  );

  expect(row("Second thread").className).toContain("current");
  expect(row("First thread").className).not.toContain("current");

  await act(async () => row("First thread").click());
  expect(location()).toBe("/threads/t-1");
});

it("hides archived threads until the switch is toggled, and archives a thread from its row action", async () => {
  await render(
    [
      thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "Active thread" }),
      thread({ id: "t-2", sandbox: "demo-a1b2", session_id: "s-2", name: "Old spike", archived: true }),
    ],
    { "demo-a1b2": sandbox("demo-a1b2") }
  );

  expect(container.textContent).not.toContain("Old spike");
  expect(container.textContent).toContain("Show archived (1)");

  const toggle = container.querySelector('input[aria-label="Show archived threads"]');
  if (!(toggle instanceof HTMLInputElement)) throw new Error("missing archived switch");
  await act(async () => toggle.click());
  expect(container.textContent).toContain("Old spike");

  const archiveButton = container.querySelector('button[aria-label="Archive Active thread"]');
  if (!(archiveButton instanceof HTMLButtonElement)) throw new Error("missing archive button");
  await act(async () => archiveButton.click());
  const archiveCall = fetchMock.mock.calls.find(
    ([request]) => new URL((request as Request).url).pathname === "/threads/t-1/archive"
  );
  expect(archiveCall).toBeDefined();
  expect((archiveCall?.[0] as Request).method).toBe("POST");
});

it("keeps the last snapshot when archiving fails and reports the error", async () => {
  await render([thread({ id: "t-1", sandbox: "demo", session_id: "s-1", name: "Keep this thread" })], {
    demo: sandbox("demo"),
  });
  fetchMock.mockResolvedValueOnce(Response.json({ detail: "Archive unavailable" }, { status: 503 }));
  const archiveButton = container.querySelector('button[aria-label="Archive Keep this thread"]');
  if (!(archiveButton instanceof HTMLButtonElement)) throw new Error("missing archive button");
  await act(async () => archiveButton.click());
  expect(row("Keep this thread")).toBeDefined();
  expect(container.textContent).toContain("Archive unavailable");
  expect(container.textContent).not.toContain("Show archived (1)");
});

function footerButton(label: string): HTMLButtonElement {
  const found = container.querySelector(`button[aria-label="${label}"]`);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

it("routes the footer icons to Sandboxes, pending approvals, Action history, and Settings", async () => {
  const { onOpenSettings } = await render([], {});

  await act(async () => footerButton("Sandboxes").click());
  expect(location()).toBe("/sandboxes");

  await act(async () => footerButton("Pending approvals").click());
  expect(location()).toBe("/actions");

  await act(async () => footerButton("Action history").click());
  expect(location()).toBe("/actions/history");

  await act(async () => footerButton("Settings").click());
  expect(onOpenSettings).toHaveBeenCalledOnce();
});

function sidebarWidth(): number {
  const nav = container.querySelector("nav.agentplane-sidebar");
  if (!(nav instanceof HTMLElement)) throw new Error("missing sidebar nav");
  return Number.parseInt(nav.style.width, 10);
}

function resizeHandle(): HTMLElement {
  const found = container.querySelector('[aria-label="Resize sidebar"]');
  if (!(found instanceof HTMLElement)) throw new Error("missing resize handle");
  return found;
}

it("defaults to 240px and widens on ArrowRight, narrows on ArrowLeft, from the resize handle", async () => {
  await render([], {});
  expect(sidebarWidth()).toBe(240);

  const handle = resizeHandle();
  await act(async () => handle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
  expect(sidebarWidth()).toBe(256);

  await act(async () => {
    handle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    handle.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
  });
  expect(sidebarWidth()).toBe(224);
});

it("clamps a dragged width to the [180, 480] range", async () => {
  await render([], {});
  const handle = resizeHandle();

  await act(async () => {
    handle.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerId: 1, clientX: 0 }));
    handle.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 1, clientX: -1000 }));
  });
  expect(sidebarWidth()).toBe(180);

  await act(async () => {
    handle.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, pointerId: 1, clientX: 1000 }));
  });
  expect(sidebarWidth()).toBe(480);
});

it("persists the resized width across a remount", async () => {
  await render([], {});
  await act(async () =>
    resizeHandle().dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }))
  );
  expect(sidebarWidth()).toBe(256);

  await act(async () => root.unmount());
  container.remove();
  await render([], {});
  expect(sidebarWidth()).toBe(256);
});

function backdrop(): HTMLElement | null {
  return container.querySelector(".agentplane-sidebar-backdrop");
}

it("renders a backdrop and the mobile-open class only while mobileOpen is true", async () => {
  await render([], {}, { mobileOpen: false });
  expect(backdrop()).toBeNull();
  expect(container.querySelector("nav.agentplane-sidebar")?.className).not.toContain("agentplane-sidebar-mobile-open");

  await act(async () => root.unmount());
  container.remove();
  await render([], {}, { mobileOpen: true });
  expect(backdrop()).not.toBeNull();
  expect(container.querySelector("nav.agentplane-sidebar")?.className).toContain("agentplane-sidebar-mobile-open");
});

it("closes the mobile drawer on backdrop click and on Escape", async () => {
  const { onMobileClose } = await render([], {}, { mobileOpen: true });

  await act(async () => backdrop()?.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  expect(onMobileClose).toHaveBeenCalledOnce();

  await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
  expect(onMobileClose).toHaveBeenCalledTimes(2);
});

it("closes the mobile drawer when opening a thread, the Sandboxes stub, or a footer icon", async () => {
  const { onMobileClose: closeOnOpenThread } = await render(
    [thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "First thread" })],
    { "demo-a1b2": sandbox("demo-a1b2") },
    { mobileOpen: true }
  );
  await act(async () => row("First thread").click());
  expect(closeOnOpenThread).toHaveBeenCalledOnce();

  await act(async () => root.unmount());
  container.remove();
  const { onMobileClose: closeOnFooter } = await render([], {}, { mobileOpen: true });
  await act(async () => footerButton("Sandboxes").click());
  expect(closeOnFooter).toHaveBeenCalledOnce();
});
