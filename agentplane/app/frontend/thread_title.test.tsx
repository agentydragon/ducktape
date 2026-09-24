// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ThreadView } from "./client";
import { ThreadTitle } from "./thread_title";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const THREAD: ThreadView = {
  id: "10000000-0000-4000-8000-000000000001",
  sandbox: "title-test",
  session_id: "session-test",
  harness: "HARNESS_CLAUDE",
  model: "test-model",
  cwd: "/test-workspace",
  created_at: "2026-01-01T00:00:00Z",
  name: "Test stored name",
  archived: false,
  last_cursor: 0,
  harness_state: "HARNESS_STATE_RUNNING",
};
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (request: Request) =>
    Response.json({ ...THREAD, ...((await request.clone().json()) as { name: string | null }) })
  );
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

async function render(thread: ThreadView | null): Promise<{
  input: HTMLInputElement;
  onRenamed: ReturnType<typeof vi.fn>;
  rerender: (next: ThreadView) => Promise<void>;
}> {
  const onRenamed = vi.fn();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const rerender = async (next: ThreadView | null): Promise<void> =>
    act(async () =>
      root.render(
        <MantineProvider env="test">
          <ThreadTitle threadId={THREAD.id} thread={next} onRenamed={onRenamed} onError={() => {}} />
        </MantineProvider>
      )
    );
  await rerender(thread);
  return { input: container.querySelector<HTMLInputElement>('input[aria-label="Thread name"]')!, onRenamed, rerender };
}

async function type(input: HTMLInputElement, text: string): Promise<void> {
  await act(async () => {
    input.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

async function press(input: HTMLInputElement, key: string): Promise<void> {
  await act(async () => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

async function renames(): Promise<Array<{ path: string; method: string; body: unknown }>> {
  return Promise.all(
    fetchMock.mock.calls.map(async ([request]) => ({
      path: new URL(request.url).pathname,
      method: request.method,
      body: await request.json(),
    }))
  );
}

it("waits for the thread before taking typing, with the thread id as its placeholder", async () => {
  const { input } = await render(null);

  expect(input.disabled).toBe(true);
  expect(input.placeholder).toBe(THREAD.id);
  expect(container.textContent).not.toContain(THREAD.id);
});

it("commits the trimmed name on Enter", async () => {
  const { input, onRenamed } = await render(THREAD);
  expect(container.textContent).toContain(THREAD.id);

  await type(input, "  Test typed name  ");
  await press(input, "Enter");

  expect(await renames()).toEqual([
    { path: `/threads/${THREAD.id}`, method: "PATCH", body: { name: "Test typed name" } },
  ]);
  expect(onRenamed).toHaveBeenCalledWith({ ...THREAD, name: "Test typed name" });
});

it("puts the stored name back on Escape and sends nothing", async () => {
  const { input } = await render(THREAD);

  await type(input, "Test discarded name");
  await press(input, "Escape");
  expect(input.value).toBe("Test stored name");
  await act(async () => input.blur());

  expect(fetchMock).not.toHaveBeenCalled();
});

it("sends nothing when the committed name is the stored one", async () => {
  const { input } = await render(THREAD);

  await type(input, " Test stored name ");
  await press(input, "Enter");

  expect(fetchMock).not.toHaveBeenCalled();
});

it("clears the name when a blank one is committed by moving away", async () => {
  const { input, onRenamed } = await render(THREAD);

  await type(input, "   ");
  await act(async () => input.blur());

  expect(await renames()).toEqual([{ path: `/threads/${THREAD.id}`, method: "PATCH", body: { name: null } }]);
  expect(onRenamed).toHaveBeenCalledWith({ ...THREAD, name: null });
});

it("shows a rename from elsewhere unless a name is being typed", async () => {
  const { input, rerender } = await render(THREAD);

  await rerender({ ...THREAD, name: "Test renamed elsewhere" });
  expect(input.value).toBe("Test renamed elsewhere");

  await type(input, "Test typed name");
  await rerender({ ...THREAD, name: "Test renamed again" });
  expect(input.value).toBe("Test typed name");
});
