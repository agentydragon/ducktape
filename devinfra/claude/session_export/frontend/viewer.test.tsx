// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { listSessionEvents, listSessions, watchSessions } from "./api";
import { SessionViewer } from "./viewer";
import { noisySession, noisySessionEvents } from "./fixtures/noisy-session";

vi.mock("./api", () => ({
  ApiError: class extends Error {},
  listSessionEvents: vi.fn(),
  listSessions: vi.fn(),
  watchSessions: vi.fn(),
}));

const session = {
  id: "session-1",
  title: "Live session",
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_event_at: "2026-01-01T00:00:00Z",
};
const secondSession = {
  ...session,
  id: "session-2",
  title: "Selected after toggling",
  updated_at: "2026-01-02T00:00:00Z",
};
const first = {
  event_id: "event-1",
  sequence_num: "1",
  event_type: "user",
  source: "client",
  created_at: "2026-01-01T00:00:00Z",
  received_at: null,
  processing_at: null,
  processed_at: null,
  device_attestation_status: "DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
  sent_by_account_id: null,
  payload: { type: "user", message: { role: "user", content: [{ type: "text", text: "First message" }] } },
};
const second = {
  ...first,
  event_id: "event-3",
  sequence_num: "3",
  payload: { type: "user", message: { role: "user", content: [{ type: "text", text: "Second message" }] } },
};
const toolCall = {
  ...first,
  event_id: "event-2",
  sequence_num: "2",
  event_type: "assistant",
  payload: {
    type: "assistant",
    message: {
      content: [{ type: "tool_use", id: "read-1", name: "Read", input: { file_path: "README.md" } }],
    },
  },
};

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;
const originalMatchMedia = Object.getOwnPropertyDescriptor(window, "matchMedia");
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  window.localStorage.removeItem("claude-session-sidebar-visible");
  window.localStorage.removeItem("claude-session-sidebar-width");
  if (originalMatchMedia === undefined)
    delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia;
  else Object.defineProperty(window, "matchMedia", originalMatchMedia);
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("keeps the selected session when the session list is hidden and shown", async () => {
  vi.mocked(listSessions).mockResolvedValue({
    data: [session, secondSession],
    next_cursor: null,
    resume_token: null,
  });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: [],
    has_more: false,
    first_id: null,
    last_id: null,
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <SessionViewer />
      </MantineProvider>
    )
  );
  await vi.waitFor(() => expect(container?.querySelector("#session-sidebar")).not.toBeNull());

  const secondRow = container.querySelector<HTMLButtonElement>('#session-sidebar button[aria-pressed="false"]')!;
  await act(async () => secondRow.click());
  await vi.waitFor(() => expect(container?.textContent).toContain("Selected after toggling"));

  await act(async () => container?.querySelector<HTMLButtonElement>('button[aria-expanded="true"]')?.click());
  expect(container.querySelector("#session-sidebar")).toBeNull();
  expect(container.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "Selected after toggling"
  );
  const showButton = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Show session list")
  );
  expect(showButton).toBeDefined();
  await act(async () => showButton?.click());
  expect(container.querySelector('#session-sidebar button[aria-pressed="true"]')?.textContent).toContain(
    "Selected after toggling"
  );
});

it("clamps pointer and keyboard resizing and restores the width after collapsing", async () => {
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: [],
    has_more: false,
    first_id: null,
    last_id: null,
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <SessionViewer />
      </MantineProvider>
    )
  );
  await vi.waitFor(() => expect(container?.querySelector('[role="separator"]')).not.toBeNull());

  const layout = container.querySelector<HTMLElement>("[data-session-viewer-layout]")!;
  Object.defineProperty(layout, "clientWidth", { configurable: true, value: 700 });
  vi.spyOn(layout, "getBoundingClientRect").mockReturnValue({
    x: 100,
    y: 0,
    left: 100,
    top: 0,
    right: 800,
    bottom: 700,
    width: 700,
    height: 700,
    toJSON: () => ({}),
  } as DOMRect);
  await act(async () => window.dispatchEvent(new Event("resize")));

  const separator = container.querySelector<HTMLElement>('[role="separator"]')!;
  expect(separator.getAttribute("aria-valuemax")).toBe("272");
  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("220");
  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("220");
  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("272");
  await act(async () => separator.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })));
  expect(separator.getAttribute("aria-valuenow")).toBe("272");

  await act(async () => separator.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 372 })));
  await act(async () => window.dispatchEvent(new MouseEvent("pointermove", { clientX: 1_000 })));
  expect(separator.getAttribute("aria-valuenow")).toBe("272");
  await act(async () => window.dispatchEvent(new MouseEvent("pointermove", { clientX: 0 })));
  expect(separator.getAttribute("aria-valuenow")).toBe("220");
  await act(async () => window.dispatchEvent(new MouseEvent("pointerup")));

  await act(async () => container?.querySelector<HTMLButtonElement>('button[aria-expanded="true"]')?.click());
  await act(async () => container?.querySelector<HTMLButtonElement>('button[aria-expanded="false"]')?.click());
  expect(container.querySelector('[role="separator"]')?.getAttribute("aria-valuenow")).toBe("220");
});

it("opens the mobile session drawer and keeps the chosen session after it closes", async () => {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((media: string) => ({
      matches: true,
      media,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  vi.mocked(listSessions).mockResolvedValue({
    data: [session, secondSession],
    next_cursor: null,
    resume_token: null,
  });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: [],
    has_more: false,
    first_id: null,
    last_id: null,
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <SessionViewer />
      </MantineProvider>
    )
  );
  await vi.waitFor(() =>
    expect(container?.querySelector('button[aria-controls="session-sidebar-mobile"]')).not.toBeNull()
  );
  const toggle = container.querySelector<HTMLButtonElement>('button[aria-controls="session-sidebar-mobile"]')!;
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  await act(async () => toggle.click());
  await vi.waitFor(() => expect(document.body.querySelector("#session-sidebar-mobile")).not.toBeNull());

  const secondRow = document.body.querySelector<HTMLButtonElement>(
    '#session-sidebar-mobile button[aria-pressed="false"]'
  )!;
  await act(async () => secondRow.click());
  await vi.waitFor(() => expect(container?.textContent).toContain("Selected after toggling"));
  expect(container.querySelector('button[aria-controls="session-sidebar-mobile"]')?.getAttribute("aria-expanded")).toBe(
    "false"
  );
});

it("retains session and transcript DOM, scroll and disclosure across watch refreshes", async () => {
  const stream = new EventTarget() as EventTarget & {
    close: () => void;
    onopen: (() => void) | null;
    onerror: (() => void) | null;
  };
  stream.close = vi.fn();
  stream.onopen = null;
  stream.onerror = null;
  vi.mocked(watchSessions).mockReturnValue(stream as unknown as EventSource);
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: "watch-1" });
  let finishRefresh: ((value: Awaited<ReturnType<typeof listSessionEvents>>) => void) | undefined;
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({ data: [first, toolCall], has_more: false, first_id: "event-1", last_id: "event-2" })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRefresh = resolve;
        })
    );

  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <SessionViewer />
      </MantineProvider>
    )
  );
  await vi.waitFor(() => expect(container?.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(1));
  const article = container.querySelector('[data-fold-kind="message"]')!;
  const list = container.querySelector('[aria-label="Session list"] button')!;
  const viewport = container.querySelector(
    '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
  ) as HTMLElement;
  viewport.scrollTop = 100;
  const toolRun = container.querySelector('[data-fold-kind="tool-run"]')!;
  const control = toolRun.querySelector<HTMLButtonElement>("[data-tool-run-toggle]")!;
  await act(async () => control.click());
  expect(control.getAttribute("aria-expanded")).toBe("true");

  await act(async () => stream.dispatchEvent(new Event("changed")));
  expect(finishRefresh).toBeDefined();
  expect(container.querySelector('[data-fold-kind="message"]')).toBe(article);
  expect(container.querySelector('[data-fold-kind="tool-run"]')).toBe(toolRun);
  expect(container.querySelector('[aria-label="Session list"] button')).toBe(list);
  expect(container.querySelector('[aria-label="Session transcript"] .mantine-ScrollArea-viewport')).toBe(viewport);
  expect(viewport.scrollTop).toBe(100);
  expect(control.getAttribute("aria-expanded")).toBe("true");
  await act(async () =>
    finishRefresh?.({ data: [first, toolCall, second], has_more: false, first_id: "event-1", last_id: "event-3" })
  );
  expect(container.querySelector('[data-fold-kind="message"]')).toBe(article);
  expect(container.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(2);
  expect(viewport.scrollTop).toBe(100);
  expect(control.getAttribute("aria-expanded")).toBe("true");
});

it("keeps suppressed events inspectable without expanding JSON or hook noise by default", async () => {
  vi.mocked(listSessions).mockResolvedValue({ data: [noisySession], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: noisySessionEvents,
    has_more: false,
    first_id: noisySessionEvents[0]!.event_id,
    last_id: noisySessionEvents.at(-1)!.event_id,
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <SessionViewer />
      </MantineProvider>
    )
  );
  await vi.waitFor(() => expect(container?.querySelector('[data-message-role="assistant"]')).not.toBeNull());
  expect(container.querySelector("[data-event-json]")).toBeNull();
  expect(container.textContent).not.toContain("fixture-check: verified sample rule");
  expect(container.querySelector('[data-fold-kind="notice"]')).toBeNull();

  await act(async () => container?.querySelector<HTMLButtonElement>('[aria-label="Show raw event stream"]')!.click());
  expect(container.querySelectorAll("[data-raw-event]")).toHaveLength(noisySessionEvents.length);
  expect(container.querySelectorAll("[data-event-json]")).toHaveLength(0);
  const kinds = container.querySelector<HTMLSelectElement>('[aria-label="Event kind"]')!;
  await act(async () => {
    kinds.value = "system · hook_response";
    kinds.dispatchEvent(new Event("change", { bubbles: true }));
  });
  const hooks = noisySessionEvents.filter((e) => e.payload.subtype === "hook_response");
  expect(container.querySelectorAll("[data-raw-event]")).toHaveLength(hooks.length);
  const detail = container.querySelector<HTMLDetailsElement>("[data-raw-event]")!;
  await act(async () => {
    detail.open = true;
    detail.dispatchEvent(new Event("toggle"));
  });
  expect(JSON.parse(container.querySelector("[data-event-json]")!.textContent!)).toEqual(hooks[0]);
  expect(container.querySelectorAll("[data-event-json]")).toHaveLength(1);

  // Search within a payload field, including records omitted from the folded view.
  const search = container.querySelector<HTMLInputElement>('[aria-label="Search event data"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "fixture-pre-0");
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(container.querySelectorAll("[data-raw-event]")).toHaveLength(1);
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "not-in-this-fixture");
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(container.textContent).toContain("No loaded events match these filters.");
  await act(async () => container?.querySelector<HTMLButtonElement>('[aria-label="Show folded transcript"]')!.click());
  expect(container.querySelector('[data-message-role="assistant"]')).not.toBeNull();
  expect(container.querySelector("[data-raw-event]")).toBeNull();
});
