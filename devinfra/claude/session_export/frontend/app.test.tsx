// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { SyncStatus } from "./schema";
import { getStatus } from "./api";
import { App } from "./app";

vi.mock("./api", () => ({
  ApiError: class extends Error {},
  getStatus: vi.fn(),
  syncNow: vi.fn(),
  startPairing: vi.fn(),
  finishPairing: vi.fn(),
}));
vi.mock("./viewer", () => ({ SessionViewer: () => <div>Session viewer</div> }));

const unpaired: SyncStatus = {
  state: "unpaired",
  pairing_started: false,
  credential: null,
  sessions: 1,
  sessions_behind: 0,
  poll_interval_seconds: 300,
  last_cycle: null,
  last_failure: null,
  live: { following: false, watching: false, streams: 0, last_event_at: null, problems: [], failure: null },
};
const paired: SyncStatus = {
  ...unpaired,
  state: "idle",
  credential: { organization_uuid: "org", scopes: [], access_token_expires_at: "2026-11-09T00:00:00Z" },
};

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;
afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.useRealTimers();
  vi.resetAllMocks();
});

async function renderApp(pathname: string): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root?.render(
      <MantineProvider env="test">
        <App pathname={pathname} />
      </MantineProvider>
    )
  );
  return container;
}

it.each(["/sessions", "/sync"])("shows a pairing link at the top of %s when unpaired", async (path) => {
  vi.mocked(getStatus).mockResolvedValue(unpaired);
  const page = await renderApp(path);
  const notice = page.querySelector('[role="alert"]');
  expect(notice?.textContent).toContain("Claude is not paired");
  expect(notice?.querySelector('a[href="/sync"]')?.textContent).toBe("Pair with Claude");
  expect(notice?.compareDocumentPosition(page.querySelector("main")!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
});

it("clears the notice when a later status reports that pairing succeeded", async () => {
  vi.useFakeTimers();
  vi.mocked(getStatus).mockResolvedValueOnce(unpaired).mockResolvedValue(paired);
  const page = await renderApp("/sessions");
  expect(page.querySelector('[role="alert"]')?.textContent).toContain("not paired");
  await act(async () => vi.advanceTimersByTimeAsync(5_000));
  expect(page.querySelector('[role="alert"]')).toBeNull();
});

it("warns about a failing sync even when an expired credential is still present", async () => {
  vi.mocked(getStatus).mockResolvedValue({
    ...paired,
    last_failure: { at: "2026-11-08T18:00:00Z", message: "Refresh token expired" },
  });
  const page = await renderApp("/sessions");
  const notice = page.querySelector('[role="alert"]');
  expect(notice?.textContent).toContain("Session sync is failing");
  expect(notice?.querySelector('a[href="/sync"]')?.textContent).toBe("Check sync status");
});
