// @vitest-environment happy-dom
import { screen, waitFor } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ThreadView } from "../client";
import { renderInMantine } from "../testing_library";
import { ThreadTitle } from "./thread_title";

const fetchMock = vi.hoisted(() => {
  const fetch = vi.fn<(request: Request) => Promise<Response>>();
  vi.stubGlobal("fetch", fetch);
  return fetch;
});

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
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockImplementation(async (request: Request) =>
    Response.json({ ...THREAD, ...((await request.clone().json()) as { name: string | null }) })
  );
});
afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function render(thread: ThreadView | null): ReturnType<typeof renderInMantine> & {
  onRenamed: ReturnType<typeof vi.fn>;
  rerenderThread: (next: ThreadView) => void;
} {
  const onRenamed = vi.fn();
  const title = (next: ThreadView | null) => (
    <ThreadTitle threadId={THREAD.id} thread={next} onRenamed={onRenamed} onError={() => {}} />
  );
  const rendered = renderInMantine(title(thread));
  return Object.assign(rendered, { onRenamed, rerenderThread: (next: ThreadView) => rendered.rerender(title(next)) });
}

/** Replaces what the field holds with `text`, typing it as a keyboard does. */
async function retype(user: UserEvent, text: string): Promise<void> {
  const input = screen.getByRole("textbox", { name: "Thread name" });
  await user.clear(input);
  await user.type(input, text);
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

it("waits for the thread before taking typing, with the thread id as its placeholder", () => {
  const { container } = render(null);
  const input = screen.getByRole("textbox", { name: "Thread name" });

  expect(input).toBeDisabled();
  expect(input).toHaveAttribute("placeholder", THREAD.id);
  expect(container).not.toHaveTextContent(THREAD.id);
});

it("commits the trimmed name on Enter", async () => {
  const { container, onRenamed, user } = render(THREAD);
  expect(container).not.toHaveTextContent(THREAD.id);

  await retype(user, "  Test typed name  {Enter}");

  await waitFor(() => expect(onRenamed).toHaveBeenCalledWith({ ...THREAD, name: "Test typed name" }));
  expect(await renames()).toEqual([
    { path: `/threads/${THREAD.id}`, method: "PATCH", body: { name: "Test typed name" } },
  ]);
});

it("puts the stored name back on Escape and sends nothing", async () => {
  const { user } = render(THREAD);

  await retype(user, "Test discarded name{Escape}");
  expect(screen.getByRole("textbox", { name: "Thread name" })).toHaveValue("Test stored name");
  await user.tab();

  expect(fetchMock).not.toHaveBeenCalled();
});

it("sends nothing when the committed name is the stored one", async () => {
  const { user } = render(THREAD);

  await retype(user, " Test stored name {Enter}");

  expect(fetchMock).not.toHaveBeenCalled();
});

it("clears the name when a blank one is committed by moving away", async () => {
  const { onRenamed, user } = render(THREAD);

  await retype(user, "   ");
  await user.tab();

  await waitFor(() => expect(onRenamed).toHaveBeenCalledWith({ ...THREAD, name: null }));
  expect(await renames()).toEqual([{ path: `/threads/${THREAD.id}`, method: "PATCH", body: { name: null } }]);
});

it("shows a rename from elsewhere unless a name is being typed", async () => {
  const { rerenderThread, user } = render(THREAD);
  const input = screen.getByRole("textbox", { name: "Thread name" });

  rerenderThread({ ...THREAD, name: "Test renamed elsewhere" });
  expect(input).toHaveValue("Test renamed elsewhere");

  await retype(user, "Test typed name");
  rerenderThread({ ...THREAD, name: "Test renamed again" });
  expect(input).toHaveValue("Test typed name");
});
