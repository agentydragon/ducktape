// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { getSession, listSessionEvents, listSessions, watchSessions } from "./api";
import { SessionViewer } from "./viewer";
import {
  longCommandActivityDetail,
  longCommandActivitySessionEvents,
  longCommandActivityTitle,
  narrationSession,
  narrationSessionEvents,
  noisySession,
  noisySessionEvents,
} from "./fixtures/noisy-session";

vi.mock("./api", () => ({
  ApiError: class extends Error {},
  getSession: vi.fn(),
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
  last_event_at: "2025-12-31T00:00:00Z",
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

function assistantEvent(sequence: number) {
  return {
    ...first,
    event_id: `event-${sequence}`,
    sequence_num: String(sequence),
    event_type: "assistant",
    payload: {
      type: "assistant",
      message: { role: "assistant", content: [{ type: "text", text: `Message ${sequence}` }] },
    },
  };
}

function eventRange(start: number, end: number, descending: boolean) {
  const firstSequence = descending ? end : start;
  const lastSequence = descending ? start : end;
  return {
    data: Array.from({ length: end - start + 1 }, (_, index) =>
      assistantEvent(descending ? end - index : start + index)
    ),
    has_more: false,
    first_id: `event-${firstSequence}`,
    last_id: `event-${lastSequence}`,
  };
}

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
  vi.useRealTimers();
  vi.resetAllMocks();
});

it("recovers an initial session-list network failure without a refresh control", async () => {
  vi.useFakeTimers();
  vi.mocked(listSessions)
    .mockRejectedValueOnce(new TypeError("temporary network failure"))
    .mockResolvedValueOnce({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({ data: [], has_more: false, first_id: null, last_id: null });
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
  await act(async () => vi.advanceTimersByTimeAsync(500));
  expect(listSessions).toHaveBeenCalledTimes(2);
  expect(container.textContent).toContain(session.title);
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Refresh")).toBe(
    false
  );
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

  const secondRow = await vi.waitFor(() => {
    const row = [
      ...(container?.querySelectorAll<HTMLButtonElement>("#session-sidebar button[aria-pressed]") ?? []),
    ].find((button) => button.textContent?.includes(secondSession.title));
    expect(row).toBeDefined();
    expect(row?.getAttribute("aria-pressed")).toBe("false");
    return row!;
  });
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

it("shows signed narration as prose while ordinary thinking remains collapsed", async () => {
  vi.mocked(listSessions).mockResolvedValue({ data: [narrationSession], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: narrationSessionEvents,
    has_more: false,
    first_id: narrationSessionEvents[0]?.event_id ?? null,
    last_id: narrationSessionEvents.at(-1)?.event_id ?? null,
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

  await vi.waitFor(() => expect(container?.querySelector('[data-fold-kind="narration"]')).not.toBeNull());
  const narration = container.querySelector<HTMLElement>('[data-fold-kind="narration"]')!;
  expect(narration.textContent).toBe("The format note is clear; I’ll verify its example next.");
  expect(narration.closest("details, summary, button, article")).toBeNull();
  expect(narration.dataset.historySequences).toBe("801");

  const rows = [
    ...container.querySelectorAll<HTMLElement>('[data-fold-kind="tool-run"], [data-fold-kind="narration"]'),
  ];
  expect(rows.map((row) => row.dataset.foldKind)).toEqual(["tool-run", "narration", "tool-run"]);
  const thinking = container.querySelector<HTMLDetailsElement>('[data-fold-kind="thinking"]')!;
  expect(thinking.open).toBe(false);
  expect(thinking.querySelector("summary")?.textContent).toBe("Thinking");
  expect(thinking.textContent).toContain("Keep this internal note folded.");
  await act(async () => {
    thinking.open = true;
  });
  expect(thinking.open).toBe(true);
  expect(thinking.textContent).toContain("Keep this internal note folded.");
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

  const secondRow = await vi.waitFor(() => {
    const row = [
      ...document.body.querySelectorAll<HTMLButtonElement>("#session-sidebar-mobile button[aria-pressed]"),
    ].find((button) => button.textContent?.includes(secondSession.title));
    expect(row).toBeDefined();
    expect(row?.getAttribute("aria-pressed")).toBe("false");
    return row!;
  });
  await act(async () => secondRow.click());
  await vi.waitFor(() => expect(container?.textContent).toContain("Selected after toggling"));
  expect(container.querySelector('button[aria-controls="session-sidebar-mobile"]')?.getAttribute("aria-expanded")).toBe(
    "false"
  );
});

it("applies committed session and transcript changes without restarting the watch", async () => {
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
  vi.mocked(getSession).mockResolvedValue({ session: { ...session, title: "Renamed live session", status: "paused" } });
  const refreshedToolCall = {
    ...toolCall,
    payload: {
      type: "assistant",
      message: {
        content: [{ type: "tool_use", id: "read-1", name: "Read", input: { file_path: "src/updated.py" } }],
      },
    },
  };
  let finishRefresh: ((value: Awaited<ReturnType<typeof listSessionEvents>>) => void) | undefined;
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({ data: [toolCall, first], has_more: false, first_id: "event-2", last_id: "event-1" })
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishRefresh = resolve;
        })
    )
    .mockResolvedValueOnce({ data: [second], has_more: false, first_id: "event-3", last_id: "event-3" });

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
  Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1000 });
  Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 400 });
  viewport.scrollTop = 100;
  await act(async () => viewport.dispatchEvent(new Event("scroll")));
  const toolRun = container.querySelector('[data-fold-kind="tool-run"]')!;
  const control = toolRun.querySelector<HTMLButtonElement>("[data-tool-run-toggle]")!;
  await act(async () => control.click());
  expect(control.getAttribute("aria-expanded")).toBe("true");

  await act(async () =>
    stream.dispatchEvent(new MessageEvent("changed", { data: JSON.stringify({ session_ids: [session.id] }) }))
  );
  expect(finishRefresh).toBeDefined();
  expect(container.querySelector('[data-fold-kind="message"]')).toBe(article);
  expect(container.querySelector('[data-fold-kind="tool-run"]')).toBe(toolRun);
  expect(container.querySelector('[aria-label="Session list"] button')).toBe(list);
  expect(container.querySelector('[aria-label="Session transcript"] .mantine-ScrollArea-viewport')).toBe(viewport);
  expect(viewport.scrollTop).toBe(100);
  expect(control.getAttribute("aria-expanded")).toBe("true");
  await vi.waitFor(() => expect(container?.querySelector("h4")?.textContent).toBe("Renamed live session"));
  expect(container.querySelector('[aria-label="Session list"]')?.textContent).toContain("paused");
  await act(async () =>
    finishRefresh?.({
      data: [second, refreshedToolCall, first],
      has_more: false,
      first_id: "event-3",
      last_id: "event-1",
    })
  );
  expect(container.querySelector('[data-fold-kind="message"]')).toBe(article);
  expect(container.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(2);
  expect(viewport.scrollTop).toBe(100);
  expect(control.getAttribute("aria-expanded")).toBe("true");
  expect(container.textContent).toContain("src/updated.py");
  expect(listSessionEvents).toHaveBeenLastCalledWith(session.id, "event-2", "asc", expect.any(AbortSignal));
  expect(getSession).toHaveBeenCalledWith(session.id, expect.any(AbortSignal));
  expect(listSessions).toHaveBeenCalledTimes(1);
  expect(watchSessions).toHaveBeenCalledTimes(1);
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.trim() === "Refresh")).toBe(
    false
  );
});

it("updates an older loaded session into and out of the active filter across a stale page response", async () => {
  const olderArchived = { ...session, id: "session-old", title: "Older loaded session", status: "archived" };
  const olderActive = { ...olderArchived, title: "Renamed older session from live feed", status: "active" };
  const olderPaused = { ...olderActive, status: "paused" };
  const newSession = { ...session, id: "session-new", title: "Brand new session from live feed", status: "active" };
  const stream = new EventTarget() as EventTarget & {
    close: () => void;
    onopen: (() => void) | null;
    onerror: (() => void) | null;
  };
  stream.close = vi.fn();
  stream.onopen = null;
  stream.onerror = null;
  vi.mocked(watchSessions).mockReturnValue(stream as unknown as EventSource);
  let finishActiveFilter: ((page: Awaited<ReturnType<typeof listSessions>>) => void) | undefined;
  vi.mocked(listSessions).mockImplementation((statuses, cursor) => {
    if (cursor !== undefined) {
      return Promise.resolve({ data: [olderArchived], next_cursor: null, resume_token: "watch-1" });
    }
    if (statuses.length === 1 && statuses[0] === "active") {
      return new Promise((resolve) => {
        finishActiveFilter = resolve;
      });
    }
    return Promise.resolve({ data: [session], next_cursor: "older-sessions", resume_token: "watch-1" });
  });
  vi.mocked(listSessionEvents).mockResolvedValue({ data: [], has_more: false, first_id: null, last_id: null });
  vi.mocked(getSession)
    .mockResolvedValueOnce({ session: olderActive })
    .mockResolvedValueOnce({ session: olderPaused })
    .mockResolvedValueOnce({ session: newSession });

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
  await vi.waitFor(() => expect(container?.textContent).toContain(session.title));
  const loadMore = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Load more sessions")
  )!;
  await act(async () => loadMore.click());
  await vi.waitFor(() => expect(container?.textContent).toContain(olderArchived.title));

  const statusLabel = [...container.querySelectorAll("label")].find((label) => label.textContent?.trim() === "Status")!;
  const statusInput = document.getElementById(statusLabel.htmlFor) as HTMLInputElement;
  await act(async () => statusInput.click());
  const activeOption = await vi.waitFor(() => {
    const option = [...document.querySelectorAll<HTMLElement>('[role="option"]')].find(
      (candidate) => candidate.textContent?.trim() === "Active"
    );
    expect(option).toBeDefined();
    return option!;
  });
  await act(async () => activeOption.click());
  await vi.waitFor(() => expect(finishActiveFilter).toBeDefined());

  await act(async () =>
    stream.dispatchEvent(new MessageEvent("changed", { data: JSON.stringify({ session_ids: [olderArchived.id] }) }))
  );
  await vi.waitFor(() => expect(container?.textContent).toContain(olderActive.title));
  await act(async () => finishActiveFilter?.({ data: [], next_cursor: null, resume_token: "watch-1" }));
  expect(container?.querySelector('#session-sidebar button[aria-pressed="true"]')?.textContent).toContain(
    olderActive.title
  );

  const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "renamed older");
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(container.querySelector('#session-sidebar button[aria-pressed="true"]')?.textContent).toContain(
    olderActive.title
  );

  await act(async () =>
    stream.dispatchEvent(new MessageEvent("changed", { data: JSON.stringify({ session_ids: [olderArchived.id] }) }))
  );
  await vi.waitFor(() => expect(getSession).toHaveBeenCalledTimes(2));
  await vi.waitFor(() => {
    const rows = [...(container?.querySelectorAll<HTMLButtonElement>("#session-sidebar button[aria-pressed]") ?? [])];
    expect(rows.some((button) => button.textContent?.includes(olderActive.title))).toBe(false);
  });
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(search, "");
    search.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () =>
    stream.dispatchEvent(new MessageEvent("changed", { data: JSON.stringify({ session_ids: [newSession.id] }) }))
  );
  await vi.waitFor(() => expect(container?.textContent).toContain(newSession.title));
  expect(watchSessions).toHaveBeenCalledTimes(1);
});

it("coalesces a burst of selected-session changes without abandoning an in-flight catch-up", async () => {
  const third = assistantEvent(4);
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
  vi.mocked(getSession).mockResolvedValue({ session });
  let finishFirstCatchup: ((page: Awaited<ReturnType<typeof listSessionEvents>>) => void) | undefined;
  let eventReads = 0;
  vi.mocked(listSessionEvents).mockImplementation((_sessionId, cursor, order) => {
    eventReads += 1;
    if (eventReads === 1)
      return Promise.resolve({ data: [first], has_more: false, first_id: first.event_id, last_id: first.event_id });
    if (eventReads === 2) {
      return new Promise((resolve) => {
        finishFirstCatchup = resolve;
      });
    }
    if (cursor === first.event_id) {
      return Promise.resolve({ data: [second], has_more: false, first_id: second.event_id, last_id: second.event_id });
    }
    if (order === "desc") {
      return Promise.resolve({
        data: [third, second, first],
        has_more: false,
        first_id: third.event_id,
        last_id: first.event_id,
      });
    }
    return Promise.resolve({ data: [third], has_more: false, first_id: third.event_id, last_id: third.event_id });
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
  await vi.waitFor(() => expect(container?.textContent).toContain("First message"));
  const changedFrame = (): MessageEvent =>
    new MessageEvent("changed", { data: JSON.stringify({ session_ids: [session.id] }) });
  await act(async () => stream.dispatchEvent(changedFrame()));
  await vi.waitFor(() => expect(finishFirstCatchup).toBeDefined());
  await act(async () => stream.dispatchEvent(changedFrame()));
  await act(async () =>
    finishFirstCatchup?.({ data: [second, first], has_more: false, first_id: second.event_id, last_id: first.event_id })
  );

  await vi.waitFor(() => expect(container?.textContent).toContain("Message 4"));
  expect(container.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(3);
  expect(eventReads).toBe(5);
  expect(listSessions).toHaveBeenCalledTimes(1);
  expect(watchSessions).toHaveBeenCalledTimes(1);
});

it("aborts per-session live detail reads when the viewer unmounts", async () => {
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
  vi.mocked(listSessionEvents).mockResolvedValue({ data: [], has_more: false, first_id: null, last_id: null });
  let detailSignal: AbortSignal | undefined;
  vi.mocked(getSession).mockImplementation((_sessionId, signal) => {
    detailSignal = signal;
    return new Promise(() => {});
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
  await vi.waitFor(() => expect(watchSessions).toHaveBeenCalledTimes(1));
  await act(async () =>
    stream.dispatchEvent(new MessageEvent("changed", { data: JSON.stringify({ session_ids: [session.id] }) }))
  );
  await vi.waitFor(() => expect(detailSignal).toBeDefined());
  await act(async () => root?.unmount());
  root = null;
  expect(detailSignal?.aborted).toBe(true);
  expect(stream.close).toHaveBeenCalledTimes(1);
});

it("loads newest events first, prepends older pages without duplication, and folds tool results across the boundary", async () => {
  const latest = {
    ...first,
    event_id: "event-4",
    sequence_num: "4",
    payload: { type: "user", message: { role: "user", content: [{ type: "text", text: "Latest message" }] } },
  };
  const toolResult = {
    ...first,
    event_id: "event-3",
    sequence_num: "3",
    payload: {
      type: "user",
      message: {
        role: "user",
        content: [
          { type: "tool_result", tool_use_id: "read-1", content: [{ type: "text", text: "Earlier file contents" }] },
        ],
      },
    },
  };
  const olderToolCall = { ...toolCall, sequence_num: "2" };
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({
      data: [latest, toolResult],
      has_more: true,
      first_id: latest.event_id,
      last_id: toolResult.event_id,
    })
    .mockResolvedValueOnce({
      data: [olderToolCall, first],
      has_more: false,
      first_id: olderToolCall.event_id,
      last_id: first.event_id,
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
  await vi.waitFor(() => expect(container?.textContent).toContain("Latest message"));
  expect(vi.mocked(listSessionEvents).mock.calls[0]?.slice(0, 3)).toEqual([session.id, undefined, "desc"]);
  expect(container.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(1);

  await act(async () => container?.querySelector<HTMLButtonElement>('[aria-label="Show raw event stream"]')!.click());
  await vi.waitFor(() => expect(container?.querySelectorAll("[data-raw-event]")).toHaveLength(2));
  const latestRawEvent = container.querySelector<HTMLDetailsElement>('[data-raw-event][data-sequence="4"]')!;
  await act(async () => {
    latestRawEvent.open = true;
    latestRawEvent.dispatchEvent(new Event("toggle"));
  });
  const loadOlder = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Load older events")
  )!;
  await act(async () => loadOlder.click());
  await vi.waitFor(() => expect(container?.querySelectorAll("[data-raw-event]")).toHaveLength(4));

  expect(vi.mocked(listSessionEvents).mock.calls[1]?.slice(0, 3)).toEqual([session.id, toolResult.event_id, "desc"]);
  expect(
    [...container.querySelectorAll<HTMLElement>("[data-raw-event]")].map((event) => event.dataset.sequence)
  ).toEqual(["1", "2", "3", "4"]);
  expect(container.querySelectorAll('[data-raw-event][data-sequence="4"]')[0]).toBe(latestRawEvent);
  expect(latestRawEvent.open).toBe(true);
  expect(container.textContent).toContain("4 of 4 loaded events");
  expect(container.textContent).not.toContain("Load older events");

  await act(async () => container?.querySelector<HTMLButtonElement>('[aria-label="Show folded transcript"]')!.click());
  const toolRun = container.querySelector('[data-fold-kind="tool-run"]')!;
  expect(toolRun.getAttribute("data-history-sequences")).toBe("2 3");
  await act(async () => toolRun.querySelector<HTMLButtonElement>("[data-tool-run-toggle]")!.click());
  expect(container.textContent).toContain("Earlier file contents");
});

it("catches up every page after a reconnect without dropping the selected older session or moving an older reader", async () => {
  const olderSession = { ...session, id: "session-older", title: "Session from an older list page" };
  const stream = new EventTarget() as EventTarget & {
    close: () => void;
    onopen: (() => void) | null;
    onerror: (() => void) | null;
  };
  stream.close = vi.fn();
  stream.onopen = null;
  stream.onerror = null;
  vi.mocked(watchSessions).mockReturnValue(stream as unknown as EventSource);
  vi.mocked(getSession).mockImplementation(async (sessionId) => ({
    session: sessionId === olderSession.id ? olderSession : session,
  }));
  vi.mocked(listSessions)
    .mockResolvedValueOnce({ data: [session], next_cursor: "older-sessions", resume_token: "watch-1" })
    .mockResolvedValueOnce({ data: [olderSession], next_cursor: null, resume_token: "watch-1" })
    .mockResolvedValueOnce({ data: [session], next_cursor: "older-sessions", resume_token: "watch-1" });
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({ ...eventRange(1, 1, true), has_more: false })
    .mockResolvedValueOnce({ ...eventRange(901, 1000, true), has_more: true })
    .mockResolvedValueOnce({ ...eventRange(1151, 1250, true), has_more: true })
    .mockResolvedValueOnce({ ...eventRange(1001, 1100, false), has_more: true })
    .mockResolvedValueOnce({ ...eventRange(1101, 1200, false), has_more: true })
    .mockResolvedValueOnce({ ...eventRange(1201, 1250, false), has_more: false });

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
  await vi.waitFor(() => expect(container?.textContent).toContain("Message 1"));
  const loadMoreSessions = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Load more sessions")
  )!;
  await act(async () => loadMoreSessions.click());
  const olderSessionButton = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes(olderSession.title)
  )!;
  await act(async () => olderSessionButton.click());
  await vi.waitFor(() => expect(container?.textContent).toContain("Message 1000"));

  const viewport = container.querySelector(
    '[aria-label="Session transcript"] .mantine-ScrollArea-viewport'
  ) as HTMLElement;
  Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 5000 });
  Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 400 });
  viewport.scrollTop = 100;
  await act(async () => viewport.dispatchEvent(new Event("scroll")));
  await act(async () => {
    stream.onerror?.();
  });
  expect(container.textContent).toContain("Reconnecting…");
  await act(async () => {
    stream.onopen?.();
    stream.dispatchEvent(new Event("reset"));
  });

  await vi.waitFor(() => expect(container?.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(350));
  expect(container.querySelector('[aria-label="Session transcript"] h4')?.textContent).toBe(olderSession.title);
  expect(container.querySelectorAll('[data-fold-kind="message"]')[0]?.getAttribute("data-history-sequences")).toBe(
    "901"
  );
  expect(container.querySelectorAll('[data-fold-kind="message"]')[349]?.getAttribute("data-history-sequences")).toBe(
    "1250"
  );
  expect(viewport.scrollTop).toBe(100);
  expect(vi.mocked(listSessionEvents).mock.calls.map((call) => call.slice(0, 3))).toEqual([
    [session.id, undefined, "desc"],
    [olderSession.id, undefined, "desc"],
    [olderSession.id, undefined, "desc"],
    [olderSession.id, "event-1000", "asc"],
    [olderSession.id, "event-1100", "asc"],
    [olderSession.id, "event-1200", "asc"],
  ]);
  expect(container.textContent).not.toContain("Could not load transcript");
  expect(getSession).toHaveBeenCalledWith(session.id, expect.any(AbortSignal));
  expect(getSession).toHaveBeenCalledWith(olderSession.id, expect.any(AbortSignal));
  expect(listSessions).toHaveBeenCalledTimes(3);
  expect(watchSessions).toHaveBeenCalledTimes(1);
});

it("keeps older-page pagination available when the newest loaded page contains only suppressed events", async () => {
  const suppressed = {
    ...first,
    event_id: "event-2",
    sequence_num: "2",
    event_type: "system",
    payload: { type: "system", subtype: "hook_response", response: { stdout: "suppressed fixture" } },
  };
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({
      data: [suppressed],
      has_more: true,
      first_id: suppressed.event_id,
      last_id: suppressed.event_id,
    })
    .mockResolvedValueOnce({ data: [first], has_more: false, first_id: first.event_id, last_id: first.event_id });

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
  await vi.waitFor(() => expect(container?.textContent).toContain("No loaded events appear in the folded transcript."));
  const loadOlder = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Load older events")
  );
  expect(loadOlder).toBeDefined();
  await act(async () => loadOlder?.click());
  await vi.waitFor(() => expect(container?.textContent).toContain("First message"));
  expect(vi.mocked(listSessionEvents).mock.calls[1]?.slice(0, 3)).toEqual([session.id, suppressed.event_id, "desc"]);
  expect(container.querySelectorAll('[data-fold-kind="message"]')).toHaveLength(1);
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

it("keeps completed activity titles compact while exposing the full title and detail when opened", async () => {
  vi.mocked(listSessions).mockResolvedValue({ data: [noisySession], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: longCommandActivitySessionEvents,
    has_more: false,
    first_id: longCommandActivitySessionEvents[0]!.event_id,
    last_id: longCommandActivitySessionEvents.at(-1)!.event_id,
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

  const activity = await vi.waitFor(() => {
    const element = [...(container?.querySelectorAll<HTMLDetailsElement>('[data-fold-kind="activity"]') ?? [])].find(
      (candidate) => candidate.querySelector("summary")?.getAttribute("title") === longCommandActivityTitle
    );
    if (element === undefined) throw new Error("Long completed activity did not render");
    return element;
  });
  const summary = activity.querySelector<HTMLElement>("summary")!;
  expect(activity.open).toBe(false);
  expect(summary.style.whiteSpace).toBe("nowrap");
  expect(summary.textContent).toContain(longCommandActivityTitle);
  await act(async () => {
    activity.open = true;
    activity.dispatchEvent(new Event("toggle"));
  });
  expect(activity.querySelector("[data-activity-title]")?.textContent).toBe(longCommandActivityTitle);
  expect(activity.querySelector("[data-activity-detail]")?.textContent).toBe(longCommandActivityDetail);
});

it("keeps an open tool disclosure when older history extends its activity group", async () => {
  const olderTool = {
    ...toolCall,
    event_id: "older-tool",
    sequence_num: "1",
    payload: {
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "older-read", name: "Read", input: { file_path: "older.md" } }] },
    },
  };
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents)
    .mockResolvedValueOnce({
      data: [toolCall],
      has_more: true,
      first_id: toolCall.event_id,
      last_id: toolCall.event_id,
    })
    .mockResolvedValueOnce({
      data: [olderTool],
      has_more: false,
      first_id: olderTool.event_id,
      last_id: olderTool.event_id,
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
  await vi.waitFor(() => expect(container?.querySelector("[data-tool-run-toggle]")).not.toBeNull());
  await act(async () => container?.querySelector<HTMLButtonElement>("[data-tool-run-toggle]")!.click());
  const loadOlder = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) =>
    button.textContent?.includes("Load older events")
  )!;
  await act(async () => loadOlder.click());
  await vi.waitFor(() =>
    expect(container?.querySelector('[data-tool-group-toggle][aria-expanded="true"]')).not.toBeNull()
  );
  const retained = container.querySelector<HTMLElement>('[data-fold-kind="tool-run"][data-history-sequences="2"]')!;
  expect(retained.querySelector('[data-tool-run-toggle][aria-expanded="true"]')).not.toBeNull();
  expect(retained.textContent).toContain("README.md");
});

it("toggles activity from header labels and status badges without collapsing on body clicks", async () => {
  const anotherTool = {
    ...toolCall,
    event_id: "another-tool",
    sequence_num: "3",
    payload: {
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "read-2", name: "Read", input: { file_path: "STYLE.md" } }] },
    },
  };
  vi.mocked(listSessions).mockResolvedValue({ data: [session], next_cursor: null, resume_token: null });
  vi.mocked(listSessionEvents).mockResolvedValue({
    data: [anotherTool, toolCall, first],
    has_more: false,
    first_id: "another-tool",
    last_id: "event-1",
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
  const group = await vi.waitFor(() => {
    const button = container!.querySelector<HTMLButtonElement>("[data-tool-group-toggle]");
    expect(button).not.toBeNull();
    return button!;
  });
  expect(group.tagName).toBe("BUTTON");
  expect(group.getAttribute("aria-expanded")).toBe("false");
  await act(async () => group.querySelector<HTMLElement>(".mantine-Text-root:nth-child(2)")!.click());
  expect(group.getAttribute("aria-expanded")).toBe("true");
  const tool = container.querySelector<HTMLButtonElement>("[data-tool-run-toggle]")!;
  expect(tool.tagName).toBe("BUTTON");
  await act(async () => tool.querySelector<HTMLElement>(".mantine-Badge-root:last-child")!.click());
  expect(tool.getAttribute("aria-expanded")).toBe("true");
  const body = tool.nextElementSibling as HTMLElement;
  await act(async () => body.click());
  expect(tool.getAttribute("aria-expanded")).toBe("true");
  expect(group.getAttribute("aria-expanded")).toBe("true");
  await act(async () => group.querySelector<HTMLElement>(".mantine-Badge-root")!.click());
  expect(group.getAttribute("aria-expanded")).toBe("false");
});
