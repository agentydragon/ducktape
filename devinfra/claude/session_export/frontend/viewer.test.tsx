// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { listSessionEvents, listSessions, watchSessions } from "./api";
import { SessionViewer } from "./viewer";

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
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.clearAllMocks();
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
