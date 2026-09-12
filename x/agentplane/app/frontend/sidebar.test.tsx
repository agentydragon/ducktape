// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { SandboxView, ThreadView } from "./client";
import { Sidebar } from "./sidebar";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
  window.localStorage.clear();
});

function sandbox(name: string, overrides: Partial<SandboxView> = {}): SandboxView {
  return {
    name,
    uid: `00000000-0000-4000-8000-${name.padStart(12, "0").slice(-12)}`,
    state: "running",
    created_at: "2026-01-01T00:00:00Z",
    operating_mode: "Running",
    conditions: [],
    ...overrides,
  };
}

function thread(overrides: Partial<ThreadView> & Pick<ThreadView, "id" | "sandbox" | "session_id">): ThreadView {
  return {
    provider: "PROVIDER_CLAUDE",
    model: "test-model",
    cwd: "/work",
    created_at: "2026-01-01T00:00:00Z",
    name: null,
    archived: false,
    last_sequence: 0,
    harness: "HARNESS_STATE_UNSPECIFIED",
    ...overrides,
  };
}

function LocationProbe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

async function render(
  threads: ThreadView[],
  sandboxes: Record<string, SandboxView>,
  options: { initialPath?: string; settingsOpen?: boolean } = {}
): Promise<{ onOpenSettings: ReturnType<typeof vi.fn> }> {
  fetchMock.mockImplementation((request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/threads/with-sandboxes") {
      return Promise.resolve(Response.json({ threads, sandboxes }));
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
  await act(async () =>
    root.render(
      <MantineProvider>
        <MemoryRouter initialEntries={[options.initialPath ?? "/"]}>
          <Sidebar settingsOpen={options.settingsOpen ?? false} onOpenSettings={onOpenSettings} />
          <Routes>
            <Route path="*" element={<LocationProbe />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return { onOpenSettings };
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

it("renders a thread whose sandbox is gone as a read-only, struck-through group with no navigation", async () => {
  await render(
    [thread({ id: "t-1", sandbox: "old-debug-3f9c", session_id: "s-9", name: "Why did the migration hang" })],
    {}
  );

  const label = [...container.querySelectorAll(".agentplane-sidebar-group-name")].find(
    (node) => node.textContent === "old-debug-3f9c"
  );
  expect(label).toBeDefined();
  expect((label as HTMLElement).style.textDecoration).toContain("line-through");

  const readonlyRow = [...container.querySelectorAll(".agentplane-sidebar-row.readonly")].find((node) =>
    node.textContent?.includes("Why did the migration hang")
  );
  expect(readonlyRow).toBeDefined();
  expect(readonlyRow?.getAttribute("role")).toBeNull();
  await act(async () => (readonlyRow as HTMLElement).click());
  expect(location()).toBe("/");
});

it("opens a thread's session route on click, and highlights the one already open", async () => {
  await render(
    [
      thread({ id: "t-1", sandbox: "demo-a1b2", session_id: "s-1", name: "First thread" }),
      thread({ id: "t-2", sandbox: "demo-a1b2", session_id: "s-2", name: "Second thread" }),
    ],
    { "demo-a1b2": sandbox("demo-a1b2") },
    { initialPath: "/sandboxes/demo-a1b2/sessions/s-2" }
  );

  expect(row("Second thread").className).toContain("current");
  expect(row("First thread").className).not.toContain("current");

  await act(async () => row("First thread").click());
  expect(location()).toBe("/sandboxes/demo-a1b2/sessions/s-1");
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

function footerButton(label: string): HTMLButtonElement {
  const found = container.querySelector(`button[aria-label="${label}"]`);
  if (!(found instanceof HTMLButtonElement)) throw new Error(`missing ${label} button`);
  return found;
}

it("routes the footer icons to pending approvals, Action history, and Settings", async () => {
  const { onOpenSettings } = await render([], {});

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
