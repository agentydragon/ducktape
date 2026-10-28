// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { SessionBrowserApi } from "./api";
import { ApiError } from "./api";
import { SessionViewer } from "./session-viewer";
import { sessionFixtureApi } from "./fixtures/session-data";
import type { EventPage, EventRecord, SessionPage, SessionSummary, SessionVersion } from "./schema";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;

afterEach(async () => {
  if (root !== null) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  vi.useRealTimers();
});

async function renderViewer(api: SessionBrowserApi, initialPath = "/"): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/" element={<SessionViewer api={api} />} />
            <Route path="/sessions/:sessionId" element={<SessionViewer api={api} />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    );
    await flush();
  });
  return container;
}

async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

function findControl(view: HTMLElement, text: string): HTMLButtonElement | HTMLAnchorElement {
  const control = [...view.querySelectorAll<HTMLButtonElement | HTMLAnchorElement>("button, a")].find((item) =>
    item.textContent?.includes(text)
  );
  if (control === undefined) throw new Error(`Could not find control containing ${JSON.stringify(text)}`);
  return control;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("opens a direct session link outside the first page and restores the requested version and filters", async () => {
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query, signal) {
      if (query.parentSessionId !== undefined) return sessionFixtureApi.listSessions(query, signal);
      return sessionFixtureApi.listSessions(query, signal).then((page) => ({
        ...page,
        data: page.data.filter((session) => session.id !== "codex-thread-1"),
      }));
    },
  };
  const view = await renderViewer(
    api,
    "/sessions/codex-thread-1?machine=thinkpad&harness=codex&version=codex-version-1"
  );

  expect(view.querySelector('select[aria-label="Filter by harness"]')).toHaveProperty("value", "codex");
  expect(view.querySelector('input[aria-label="Filter by machine"]')).toHaveProperty("value", "ThinkPad");
  expect(view.querySelector('select[aria-label="Captured version"]')).toHaveProperty("value", "codex-version-1");
  expect(view.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "This is the earlier captured version."
  );
});

it("keeps session selection in history for back and forward navigation", async () => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const router = createMemoryRouter(
    [
      { path: "/", element: <SessionViewer api={sessionFixtureApi} /> },
      { path: "/sessions/:sessionId", element: <SessionViewer api={sessionFixtureApi} /> },
    ],
    { initialEntries: ["/sessions/codex-thread-1"] }
  );
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <RouterProvider router={router} />
      </MantineProvider>
    );
    await flush();
  });
  await act(async () => {
    findControl(container!, "Inspect a local transcript").click();
    await flush();
  });
  expect(router.state.location.pathname).toBe("/sessions/claude-session-1");
  expect(container.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "Review this local conversation."
  );

  await act(async () => {
    await router.navigate(-1);
    await flush();
  });
  expect(router.state.location.pathname).toBe("/sessions/codex-thread-1");
  expect(container.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "I found two snapshots for this session."
  );

  await act(async () => {
    await router.navigate(1);
    await flush();
  });
  expect(router.state.location.pathname).toBe("/sessions/claude-session-1");
  expect(container.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "Review this local conversation."
  );
});

it("keeps a missing session link visible instead of silently selecting another session", async () => {
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    getSession(sessionId, signal) {
      if (sessionId === "missing") return Promise.reject(new ApiError(404, "Session not found"));
      return sessionFixtureApi.getSession(sessionId, signal);
    },
  };
  const view = await renderViewer(api, "/sessions/missing");
  expect(view.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "Could not load session missing: Session not found"
  );
  expect(view.querySelector('[aria-label="Session transcript"]')?.textContent).not.toContain(
    "I found two snapshots for this session."
  );
});

it("renders session and related-session navigation as shareable links without carrying the old version", async () => {
  const view = await renderViewer(
    sessionFixtureApi,
    "/sessions/claude-session-1?machine=atlas&harness=claude&version=claude-version-1"
  );
  const sidebar = view.querySelector('[aria-label="Session list"]');
  const parentLink = [...view.querySelectorAll<HTMLAnchorElement>('[aria-label="Related sessions"] a')].find((link) =>
    link.textContent?.includes("Inferred Claude parent")
  );
  const sidebarParentLink = [...(sidebar?.querySelectorAll<HTMLAnchorElement>("a") ?? [])].find((link) =>
    link.textContent?.includes("Inferred Claude parent")
  );
  const expectedParentPath = "/sessions/inferred-claude-parent?machine=atlas&harness=claude";
  expect(parentLink?.getAttribute("href")).toBe(expectedParentPath);
  expect(sidebarParentLink?.getAttribute("href")).toBe(expectedParentPath);
  expect(sidebar?.querySelector('a[aria-current="page"]')?.getAttribute("href")).toBe(
    "/sessions/claude-session-1?machine=atlas&harness=claude"
  );

  await act(async () => {
    parentLink!.click();
    await flush();
  });
  const childLink = [...view.querySelectorAll<HTMLAnchorElement>('[aria-label="Related sessions"] a')].find((link) =>
    link.textContent?.includes("Inspect a local transcript")
  );
  expect(childLink?.getAttribute("href")).toBe("/sessions/claude-session-1?machine=atlas&harness=claude");
});

it("does not show a different transcript for an unavailable version URL", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/codex-thread-1?version=missing-version");
  const transcript = view.querySelector('[aria-label="Session transcript"]');
  expect(view.querySelector('select[aria-label="Captured version"]')).toHaveProperty("value", "missing-version");
  expect(transcript?.textContent).toContain("Requested capture unavailable");
  expect(transcript?.textContent).not.toContain("I found two snapshots for this session.");

  await act(async () => {
    findControl(view, "View current capture").click();
    await flush();
  });
  expect(transcript?.textContent).toContain("I found two snapshots for this session.");
});

it("shows captured machines, readable records, malformed raw lines, and older versions", async () => {
  const view = await renderViewer(sessionFixtureApi);

  expect(view.textContent).toContain("ThinkPad");
  expect(view.textContent).toContain("Import progress by machine");
  expect(view.textContent).toContain("3 of 5 registered complete");
  expect(view.textContent).toContain("Oldest unfinished snapshot:");
  expect(view.textContent).toContain("File processing pending");
  expect(view.textContent).toContain("Codex metadata pending");
  expect(view.textContent).toContain("Codex metadata failed");
  expect(view.textContent).toContain("Codex metadata: 1 pending · 0 failed · 1 partial");
  expect(view.textContent).toContain("completed snapshots can have partial metadata, shown separately");
  expect(view.textContent).toContain("27 processed · 4 pending · 0 failed");
  expect(view.textContent).toContain("11 processed · 0 pending · 1 failed");
  expect(view.textContent).toContain("Snapshot counts include registered snapshots");
  expect(view.textContent).toContain("can grow as more are discovered");
  expect(view.textContent).toContain("File counts cover entries inventoried so far");
  expect(view.textContent).toContain("Compare two captured versions");
  expect(view.textContent).toContain("I found two snapshots for this session.");
  expect(view.textContent).toContain("Latest captured");

  const malformed = view.querySelector<HTMLElement>('[data-event-ordinal="3"]');
  expect(malformed?.textContent).toContain("Malformed JSONL");
  const rawDisclosure = malformed?.querySelector<HTMLDetailsElement>("details");
  expect(rawDisclosure).not.toBeNull();
  rawDisclosure!.open = true;
  expect(rawDisclosure!.querySelector("pre")?.textContent).toBe('{"type":"response_item","payload":');

  await act(async () => {
    findControl(view, "Load more records").click();
    await flush();
  });
  const structured = view.querySelector('[data-event-ordinal="4"]');
  expect(structured?.textContent).toContain("Codex · context compacted");
  expect(structured?.textContent).toContain("Structured record; expand raw data to inspect it.");

  const version = view.querySelector<HTMLSelectElement>('select[aria-label="Captured version"]');
  expect(version).not.toBeNull();
  await act(async () => {
    version!.value = "codex-version-1";
    version!.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
  });
  expect(view.textContent).toContain("This is the earlier captured version.");
  expect(view.textContent).not.toContain("I found two snapshots for this session.");
});

it("distinguishes inventory, failed and missing imports from an empty captured version", async () => {
  function summary(
    id: string,
    title: string,
    transcript_status: SessionSummary["transcript_status"],
    version_count = 0,
    latest_version_id: string | null = null,
    latest_version_record_count: number | null = null
  ): SessionSummary {
    return {
      id,
      machine_id: "thinkpad",
      harness: "codex",
      title,
      cwd: null,
      started_at: null,
      last_activity_at: null,
      latest_version_id,
      version_count,
      latest_version_record_count,
      transcript_status,
      parent_session_id: null,
      parent_link_kind: null,
      parent_native_session_id: null,
      identity_inferred: false,
    };
  }

  const sessions = [
    summary("pending", "Inventory-only placeholder", "pending"),
    summary("failed", "Failed import placeholder", "failed"),
    summary("unavailable", "Unavailable source placeholder", "unavailable"),
    summary("empty", "Captured empty transcript", "captured", 1, "empty-version", 0),
  ];
  const emptyVersion: SessionVersion = {
    id: "empty-version",
    captured_at: "2026-10-28T17:00:00Z",
    sha256: "a".repeat(64),
    byte_size: 0,
    source_path: "/archive/empty.jsonl",
    complete: true,
    parse_issue_count: 0,
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions() {
      return Promise.resolve({ data: sessions, next_cursor: null });
    },
    getSession(sessionId) {
      const session = sessions.find(({ id }) => id === sessionId);
      if (session === undefined) throw new Error(`Unknown fixture session ${sessionId}`);
      return Promise.resolve(session);
    },
    listVersions(sessionId) {
      return Promise.resolve(sessionId === "empty" ? [emptyVersion] : []);
    },
    listEvents() {
      return Promise.resolve({ data: [], next_cursor: null });
    },
  };
  const view = await renderViewer(api);

  expect(findControl(view, "Inventory-only placeholder").textContent).toContain("Inventory only");
  expect(findControl(view, "Failed import placeholder").textContent).toContain("Import failed");
  expect(findControl(view, "Unavailable source placeholder").textContent).toContain("No captured version");
  expect(findControl(view, "Captured empty transcript").textContent).toContain("Latest capture empty");
  expect(view.textContent).toContain("Restic inventory found a file, but no transcript version has been imported yet.");

  await act(async () => {
    findControl(view, "Failed import placeholder").click();
    await flush();
  });
  expect(view.textContent).toContain("Importing an inventoried file failed, so no transcript version is available.");

  await act(async () => {
    findControl(view, "Unavailable source placeholder").click();
    await flush();
  });
  expect(view.textContent).toContain("Inventory alone does not establish that a usable file was captured");

  await act(async () => {
    findControl(view, "Captured empty transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("This captured version contains no JSONL records.");
});

it("refreshes import progress on demand and polls automatically", async () => {
  const fixtureProgress = await sessionFixtureApi.listImportProgress();
  let requests = 0;
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listImportProgress() {
      requests += 1;
      return Promise.resolve(
        fixtureProgress.map((machine) => ({
          ...machine,
          processed_file_count: 26 + requests,
        }))
      );
    },
  };
  vi.useFakeTimers();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <Routes>
            <Route path="/" element={<SessionViewer api={api} />} />
            <Route path="/sessions/:sessionId" element={<SessionViewer api={api} />} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    );
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(requests).toBe(1);
  expect(container.textContent).toContain("27 processed");

  await act(async () => {
    findControl(container!, "Refresh").click();
    await Promise.resolve();
  });
  expect(requests).toBe(2);
  expect(container.textContent).toContain("28 processed");

  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(requests).toBe(3);
  expect(container.textContent).toContain("29 processed");
});

it("lets the session list refresh separately from import progress", async () => {
  let sessionListRequests = 0;
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query, signal) {
      sessionListRequests += 1;
      return sessionFixtureApi.listSessions(query, signal);
    },
  };
  const view = await renderViewer(api);
  const requestsBeforeRefresh = sessionListRequests;

  await act(async () => {
    findControl(view, "Refresh session list").click();
    await flush();
  });

  expect(sessionListRequests).toBe(requestsBeforeRefresh + 1);
  expect(view.textContent).toContain("Progress refreshes every 10 seconds; refresh sessions separately");
});

it("shows inferred subagent links and navigates to a placeholder parent", async () => {
  const view = await renderViewer(sessionFixtureApi);

  await act(async () => {
    findControl(view, "Inspect a local transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("Parent inferred from Claude subagent path");
  expect(view.textContent).toContain("Inferred Claude parent · session 12ab");

  await act(async () => {
    findControl(view, "Inferred Claude parent · session 12ab").click();
    await flush();
  });
  expect(view.textContent).toContain("No transcript version is available. Inventory alone does not establish");
  expect(view.textContent).toContain("Parent inferred from Claude subagent path");
  expect(view.textContent).toContain("Inspect a local transcript");

  await act(async () => {
    findControl(view, "Inspect a local transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("Review this local conversation.");
});

it("shows unresolved native parent metadata without inventing a session link", async () => {
  const unresolvedSession: SessionSummary = {
    id: "codex-child-unresolved",
    machine_id: "thinkpad",
    harness: "codex",
    title: "Inspect native child transcript",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: null,
    parent_link_kind: "codex_parent_thread_id_unresolved",
    parent_native_session_id: "thread-parent-123",
    identity_inferred: false,
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query) {
      if (query.parentSessionId !== undefined) return Promise.resolve({ data: [], next_cursor: null });
      return Promise.resolve({ data: [unresolvedSession], next_cursor: null });
    },
  };
  const view = await renderViewer(api);

  expect(view.textContent).toContain("Codex parent_thread_id is unresolved");
  expect(view.textContent).toContain("Native parent ID: thread-parent-123");
  expect(view.textContent).toContain("Parent session could not be resolved to a captured session.");
});

it("shows conflicting Codex native and parent IDs without inventing a parent link", async () => {
  const conflictingSession: SessionSummary = {
    id: "codex-child-conflicting",
    machine_id: "thinkpad",
    harness: "codex",
    title: "Inspect conflicting thread metadata",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: null,
    parent_link_kind: "codex_parent_thread_id_inconsistent",
    parent_native_session_id: null,
    identity_inferred: false,
    native_session_id_conflict: true,
    native_session_id_candidates: ["thread-child-a", "thread-child-b"],
    parent_native_session_id_conflict: true,
    parent_native_session_id_candidates: ["thread-parent-a", "thread-parent-b"],
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query) {
      if (query.parentSessionId !== undefined) return Promise.resolve({ data: [], next_cursor: null });
      return Promise.resolve({ data: [conflictingSession], next_cursor: null });
    },
  };
  const view = await renderViewer(api);

  expect(view.textContent).toContain("Conflicting native session IDs");
  expect(view.textContent).toContain("Native session ID candidates: thread-child-a, thread-child-b");
  expect(view.textContent).toContain("Conflicting Codex parent_thread_id values");
  expect(view.textContent).toContain("Conflicting native parent ID candidates: thread-parent-a, thread-parent-b");
  expect(view.textContent).toContain("Parent session could not be resolved to a captured session.");
});

it("labels a resolved Codex parent_thread_id link and navigates to the captured parent", async () => {
  const parent: SessionSummary = {
    id: "codex-parent-session",
    machine_id: "thinkpad",
    harness: "codex",
    title: "Captured Codex parent",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: null,
    identity_inferred: false,
  };
  const child: SessionSummary = {
    ...parent,
    id: "codex-child-session",
    title: "Captured Codex child",
    parent_session_id: parent.id,
    parent_link_kind: "codex_parent_thread_id",
    parent_native_session_id: "thread-parent-123",
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query) {
      return Promise.resolve({ data: query.parentSessionId === undefined ? [child] : [], next_cursor: null });
    },
    getSession(sessionId) {
      if (sessionId === parent.id) return Promise.resolve(parent);
      throw new Error(`Unknown fixture session ${sessionId}`);
    },
  };
  const view = await renderViewer(api);

  expect(view.textContent).toContain("Parent linked from Codex parent_thread_id");
  expect(view.textContent).toContain("Native parent ID: thread-parent-123");
  await act(async () => {
    findControl(view, "Captured Codex parent").click();
    await flush();
  });
  expect(view.textContent).toContain("Captured Codex parent");
  expect(view.querySelector('[aria-label="Related sessions"]')).toBeNull();
});

it("does not append an event page after switching sessions", async () => {
  const pending = deferred<EventPage<EventRecord>>();
  const eventRequests: Array<[string, string]> = [];
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listEvents(sessionId, versionId, cursor, signal) {
      eventRequests.push([sessionId, versionId]);
      if (sessionId === "codex-thread-1" && cursor === 3) return pending.promise;
      return sessionFixtureApi.listEvents(sessionId, versionId, cursor, signal);
    },
  };
  const view = await renderViewer(api);

  await act(async () => {
    findControl(view, "Load more records").click();
    await Promise.resolve();
  });
  await act(async () => {
    findControl(view, "Inspect a local transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("Review this local conversation.");

  pending.resolve({
    data: [
      {
        ordinal: 5,
        timestamp: "2026-10-28T16:21:00Z",
        kind: "assistant",
        parsed: { type: "assistant", message: { content: [{ type: "text", text: "Stale page content" }] } },
        raw_base64: btoa('{"type":"assistant","message":{"content":[{"type":"text","text":"Stale page content"}]}}'),
        parse_error: null,
      },
    ],
    next_cursor: null,
  });
  await act(async () => flush());

  expect(view.textContent).toContain("Review this local conversation.");
  expect(eventRequests).toContainEqual(["claude-session-1", "claude-version-1"]);
  expect(eventRequests).not.toContainEqual(["claude-session-1", "codex-version-2"]);
  expect(view.textContent).not.toContain("Stale page content");
});

it("does not append a session page after changing the harness filter", async () => {
  const pending = deferred<SessionPage<SessionSummary>>();
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listSessions(query, signal) {
      if (query.cursor !== undefined) return pending.promise;
      const page = await sessionFixtureApi.listSessions(query, signal);
      return { data: page.data.slice(0, 1), next_cursor: query.harness === undefined ? "older" : null };
    },
  };
  const view = await renderViewer(api);

  await act(async () => {
    findControl(view, "Load more sessions").click();
    await Promise.resolve();
  });
  const harness = view.querySelector<HTMLSelectElement>('select[aria-label="Filter by harness"]');
  expect(harness).not.toBeNull();
  await act(async () => {
    harness!.value = "claude";
    harness!.dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
  });
  expect(view.textContent).toContain("Inspect a local transcript");

  pending.resolve({
    data: [
      {
        id: "stale-session",
        machine_id: "thinkpad",
        harness: "codex",
        title: "Stale session page",
        cwd: null,
        started_at: null,
        last_activity_at: null,
        latest_version_id: null,
        version_count: 0,
        latest_version_record_count: null,
        transcript_status: "unavailable",
        parent_session_id: null,
        parent_link_kind: null,
        parent_native_session_id: null,
        identity_inferred: false,
      },
    ],
    next_cursor: null,
  });
  await act(async () => flush());

  expect(view.textContent).not.toContain("Stale session page");
  expect(view.textContent).toContain("Inspect a local transcript");
});
