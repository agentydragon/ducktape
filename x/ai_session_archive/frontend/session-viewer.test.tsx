// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { createMemoryRouter, MemoryRouter, RouterProvider } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { SessionBrowserApi } from "./api";
import { ApiError, httpApi } from "./api";
import { App } from "./app";
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
  localStorage.removeItem("ai-session-archive-sidebar-width");
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function renderViewer(api: SessionBrowserApi, initialPath = "/"): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[initialPath]}>
          <App api={api} />
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

async function scrollToEarlierRecords(view: HTMLElement): Promise<void> {
  const history = view.querySelector<HTMLElement>('[role="region"][aria-label="Conversation records"]')!;
  // Happy DOM has no layout; supply a visible viewport at the earlier-page boundary.
  const original = HTMLElement.prototype.getBoundingClientRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.closest('[aria-label="Conversation records"]') ? new DOMRect(0, 0, 640, 40) : original.call(this);
  });
  Object.defineProperty(history, "clientHeight", { configurable: true, value: 200 });
  await act(async () => {
    history.scrollTop = 0;
    history.dispatchEvent(new Event("scroll"));
    await flush();
  });
  await vi.waitFor(async () => {
    await act(async () => {
      await flush();
    });
    expect(history.getAttribute("data-layout-settled")).toBe("true");
  });
}

async function toggleKind(view: HTMLElement, kind: string): Promise<void> {
  const checkbox = view.querySelector<HTMLInputElement>(`input[type="checkbox"][value="${kind}"]`);
  if (checkbox === null) throw new Error(`Missing session-kind checkbox ${kind}`);
  await act(async () => {
    checkbox.click();
    await flush();
  });
}

it("hides guardian and subagent sessions by default and restores them with checkboxes", async () => {
  const requests = vi.fn(sessionFixtureApi.listSessions);
  const view = await renderViewer({ ...sessionFixtureApi, listSessions: requests });
  const sidebar = view.querySelector<HTMLElement>("#session-browser-sidebar")!;
  expect(sidebar.textContent).toContain("Compare two captured versions");
  expect(sidebar.textContent).not.toContain("Guardian automation fixture");
  expect(sidebar.textContent).not.toContain("Inspect a local transcript");
  expect(requests.mock.calls[0]?.[0].kinds).toEqual(["main", "unknown"]);

  await toggleKind(view, "guardian");
  expect(sidebar.textContent).toContain("Guardian automation fixture");
  expect(sidebar.textContent).not.toContain("Inspect a local transcript");
  await toggleKind(view, "subagent");
  expect(sidebar.textContent).toContain("Inspect a local transcript");
});

it("restores kind filters from the URL and permits an empty selection without fetching all kinds", async () => {
  const requests = vi.fn(sessionFixtureApi.listSessions);
  const view = await renderViewer({ ...sessionFixtureApi, listSessions: requests }, "/?kind=guardian");
  const sidebar = view.querySelector<HTMLElement>("#session-browser-sidebar")!;
  expect(sidebar.textContent).toContain("Guardian automation fixture");
  expect(sidebar.textContent).not.toContain("Compare two captured versions");
  const listCalls = (): number => requests.mock.calls.filter(([query]) => query.parentSessionId === undefined).length;
  const previousCalls = listCalls();
  await toggleKind(view, "guardian");
  expect(sidebar.textContent).toContain("No captured sessions match these filters.");
  expect(listCalls()).toBe(previousCalls);
});

it("opens hidden session kinds through direct and related links without filtering related children", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/guardian-session");
  const sidebar = view.querySelector<HTMLElement>("#session-browser-sidebar")!;
  expect(sidebar.textContent).not.toContain("Guardian automation fixture");
  expect(view.querySelector("h2")?.textContent).toBe("Guardian automation fixture");
  const related = await openRelatedSessions(view);
  await act(async () => {
    findControl(related, "Compare two captured versions").click();
    await flush();
  });
  const parentRelated = await openRelatedSessions(view);
  expect(parentRelated.textContent).toContain("Guardian automation fixture");
});

it("sends each selected session kind as a repeated API parameter", async () => {
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify({ data: [], next_cursor: null })));
  try {
    await httpApi.listSessions({ kinds: ["main", "guardian"], limit: 2 });
    const url = new URL(String(fetchMock.mock.calls[0]?.[0]), "https://archive.example");
    expect(url.searchParams.getAll("kind")).toEqual(["main", "guardian"]);
    expect(url.searchParams.get("limit")).toBe("2");
  } finally {
    fetchMock.mockRestore();
  }
});

async function openRelatedSessions(view: HTMLElement): Promise<HTMLElement> {
  const trigger = view.querySelector<HTMLButtonElement>(".session-browser-related-trigger");
  if (trigger === null) throw new Error("Could not find the related sessions trigger");
  await act(async () => {
    trigger.click();
    await flush();
  });
  const related = document.body.querySelector<HTMLElement>(
    '#session-browser-related-dropdown[aria-label="Related sessions"]'
  );
  if (related === null) throw new Error("Could not find the related sessions panel");
  return related;
}

function findControl(view: HTMLElement, text: string): HTMLButtonElement | HTMLAnchorElement {
  const control = [...view.querySelectorAll<HTMLButtonElement | HTMLAnchorElement>("button, a")].find(
    (item) => item.textContent?.includes(text) || item.getAttribute("aria-label") === text
  );
  if (control === undefined) throw new Error(`Could not find control containing ${JSON.stringify(text)}`);
  return control;
}

async function toggleConversationOptions(view: HTMLElement): Promise<void> {
  await act(async () => {
    findControl(view, "Conversation options").click();
    await flush();
  });
}

function findVersionMenuItem(view: HTMLElement, versionId: string): HTMLButtonElement {
  const trigger = findControl(view, "Conversation options");
  const menuId = trigger.getAttribute("aria-controls");
  if (trigger.getAttribute("aria-expanded") !== "true" || menuId === null) {
    throw new Error("Conversation options menu is not open");
  }
  const item = document
    .getElementById(menuId)
    ?.querySelector<HTMLButtonElement>(`[role="menuitemradio"][data-version-id="${versionId}"]`);
  if (item == null) throw new Error(`Could not find captured-version menu item ${JSON.stringify(versionId)}`);
  return item;
}

function findProgressBar(view: HTMLElement, machineName: string): HTMLElement {
  const progressBar = [...view.querySelectorAll<HTMLElement>('[role="progressbar"]')].find(
    (item) => item.getAttribute("aria-label") === `${machineName} file-entry progress`
  );
  if (progressBar === undefined) throw new Error(`Could not find file-entry progress for ${machineName}`);
  return progressBar;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("resizes the session list from its accessible separator", async () => {
  const view = await renderViewer(sessionFixtureApi);
  const separator = view.querySelector<HTMLElement>('[role="separator"][aria-label="Resize session list"]');
  expect(separator).not.toBeNull();
  expect(separator?.getAttribute("aria-controls")).toBe("session-browser-sidebar");
  expect(separator?.getAttribute("aria-valuenow")).toBe("320");

  await act(async () => {
    separator!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  });
  expect(separator?.getAttribute("aria-valuenow")).toBe("336");

  await act(async () => {
    separator!.dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
  });
  expect(separator?.getAttribute("aria-valuenow")).toBe("520");
});

it("opens and closes the mobile session list with Escape", async () => {
  const view = await renderViewer(sessionFixtureApi);
  const toggle = view.querySelector<HTMLButtonElement>(".session-browser-mobile-toggle");
  const sidebar = view.querySelector<HTMLElement>("#session-browser-sidebar");
  expect(toggle).not.toBeNull();
  expect(sidebar).not.toBeNull();
  expect(toggle?.getAttribute("aria-expanded")).toBe("false");

  await act(async () => toggle!.click());
  expect(toggle?.getAttribute("aria-expanded")).toBe("true");
  expect(sidebar?.classList.contains("session-browser-sidebar-open")).toBe(true);

  await act(async () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  });
  expect(toggle?.getAttribute("aria-expanded")).toBe("false");
  expect(sidebar?.classList.contains("session-browser-sidebar-open")).toBe(false);
});

it("opens related sessions from the title and counts the represented parent", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/claude-session-1");
  await act(async () => {
    await flush();
    await flush();
  });

  const toggle = view.querySelector<HTMLButtonElement>(".session-browser-related-trigger");
  expect(toggle?.getAttribute("aria-label")).toBe("Related sessions (1 shown)");
  expect(toggle?.querySelector(".session-browser-related-count")?.textContent).toBe("1");
  expect(toggle?.getAttribute("aria-expanded")).toBe("false");

  const related = await openRelatedSessions(view);
  expect(toggle?.getAttribute("aria-expanded")).toBe("true");
  expect(related.textContent).toContain("Parent inferred from Claude subagent path");
  expect(related.textContent).toContain("Inferred Claude parent · session 12ab");
});

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
  await toggleConversationOptions(view);
  expect(findVersionMenuItem(view, "codex-version-1").getAttribute("aria-checked")).toBe("true");
  expect(view.querySelector('[aria-label="Session transcript"]')?.textContent).toContain(
    "This is the earlier captured version."
  );
});

it("keeps session selection in history for back and forward navigation", async () => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const router = createMemoryRouter([{ path: "*", element: <App api={sessionFixtureApi} /> }], {
    initialEntries: ["/sessions/codex-thread-1?kind=main&kind=subagent&kind=unknown"],
  });
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
    "/sessions/claude-session-1?machine=atlas&harness=claude&kind=main&kind=subagent&version=claude-version-1"
  );
  await openRelatedSessions(view);
  const sidebar = view.querySelector('[aria-label="Session list"]');
  const parentLink = [...document.body.querySelectorAll<HTMLAnchorElement>('[aria-label="Related sessions"] a')].find(
    (link) => link.textContent?.includes("Inferred Claude parent")
  );
  const sidebarParentLink = [...(sidebar?.querySelectorAll<HTMLAnchorElement>("a") ?? [])].find((link) =>
    link.textContent?.includes("Inferred Claude parent")
  );
  const expectedParentPath = "/sessions/inferred-claude-parent?machine=atlas&harness=claude&kind=main&kind=subagent";
  expect(parentLink?.getAttribute("href")).toBe(expectedParentPath);
  expect(sidebarParentLink?.getAttribute("href")).toBe(expectedParentPath);
  expect(sidebar?.querySelector('a[aria-current="page"]')?.getAttribute("href")).toBe(
    "/sessions/claude-session-1?machine=atlas&harness=claude&kind=main&kind=subagent"
  );

  await act(async () => {
    parentLink!.click();
    await flush();
  });
  expect(view.querySelector<HTMLButtonElement>(".session-browser-related-trigger")?.getAttribute("aria-expanded")).toBe(
    "false"
  );
  await openRelatedSessions(view);
  const childLink = [...document.body.querySelectorAll<HTMLAnchorElement>('[aria-label="Related sessions"] a')].find(
    (link) => link.textContent?.includes("Inspect a local transcript")
  );
  expect(childLink?.getAttribute("href")).toBe(
    "/sessions/claude-session-1?machine=atlas&harness=claude&kind=main&kind=subagent"
  );
});

it("does not show a different transcript for an unavailable version URL", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/codex-thread-1?version=missing-version");
  const transcript = view.querySelector('[aria-label="Session transcript"]');
  expect(transcript?.textContent).toContain("Requested capture unavailable");
  expect(transcript?.textContent).not.toContain("I found two snapshots for this session.");
  await toggleConversationOptions(view);
  expect(document.body.textContent).toContain("Requested capture unavailable");
  await toggleConversationOptions(view);

  await act(async () => {
    findControl(view, "View current capture").click();
    await flush();
  });
  expect(transcript?.textContent).toContain("I found two snapshots for this session.");
});

it("shows captured machines, readable records, malformed raw lines, and older versions", async () => {
  const view = await renderViewer(sessionFixtureApi);

  expect(view.textContent).toContain("ThinkPad");
  expect(view.querySelector('header [aria-label="Import progress by machine"]')).not.toBeNull();
  expect(view.querySelector('[aria-label="Syncing ThinkPad"]')).toBeNull();
  expect(view.textContent).toContain("27/31");
  expect(view.textContent).toContain("11/12 · 1 failed");
  expect(view.textContent).toContain("72/198 snapshots remaining");
  expect(view.textContent).toContain("Rugged");
  expect(view.querySelector('[role="status"][aria-label="Complete fixture machine synced"]')).not.toBeNull();
  expect(view.querySelector('[role="progressbar"][aria-label^="Complete fixture machine"]')).toBeNull();
  expect(view.textContent).not.toContain("Snapshots: 3 of 5 registered complete");
  expect(findControl(view, "Import details").getAttribute("href")).toBe("/?view=import-progress");

  const thinkpadProgress = findProgressBar(view, "ThinkPad");
  expect(thinkpadProgress.getAttribute("aria-valuenow")).toBe("27");
  expect(thinkpadProgress.getAttribute("aria-valuemax")).toBe("31");
  expect(thinkpadProgress.getAttribute("aria-valuetext")).toBe(
    "27 processed, 4 pending, 0 failed of 31 inventoried file entries"
  );
  const atlasProgress = findProgressBar(view, "Atlas");
  expect(atlasProgress.getAttribute("aria-valuenow")).toBe("11");
  expect(atlasProgress.getAttribute("aria-valuemax")).toBe("12");
  expect(atlasProgress.getAttribute("aria-valuetext")).toBe(
    "11 processed, 0 pending, 1 failed of 12 inventoried file entries"
  );
  expect(atlasProgress.classList.contains("session-import-machine-progress-active")).toBe(false);
  const snapshotProgress = view.querySelector<HTMLElement>(
    '[role="progressbar"][aria-label="Rugged snapshot progress"]'
  );
  expect(snapshotProgress?.getAttribute("aria-valuenow")).toBe("126");
  expect(snapshotProgress?.getAttribute("aria-valuemax")).toBe("198");
  expect(snapshotProgress?.getAttribute("aria-valuetext")).toBe("126 of 198 registered snapshots complete, 0 failed");
  expect(snapshotProgress?.classList.contains("session-import-machine-progress-active")).toBe(true);
  expect(snapshotProgress?.querySelector(".session-import-machine-progress-pending")).not.toBeNull();
  expect(view.textContent).toContain("Compare two captured versions");
  expect(view.textContent).toContain("I found two snapshots for this session.");
  await toggleConversationOptions(view);
  const latestCapture = findVersionMenuItem(view, "codex-version-2");
  expect(latestCapture.getAttribute("aria-checked")).toBe("true");
  expect(latestCapture.textContent).toContain("Latest captured");
  await toggleConversationOptions(view);

  const malformed = view.querySelector<HTMLElement>('[data-event-ordinal="3"]');
  expect(malformed?.textContent).toContain("Malformed JSONL");
  await act(async () => findControl(malformed!, "Raw JSONL record").click());
  expect(malformed!.querySelector(".cm-content")?.textContent).toBe('{"type":"response_item","payload":');

  expect(view.querySelector('[data-event-ordinal="1"]')).toBeNull();
  await scrollToEarlierRecords(view);
  expect(view.querySelector('[data-event-ordinal="1"]')).not.toBeNull();
  const structured = view.querySelector('[data-event-ordinal="4"]');
  expect(structured?.textContent).toContain("Context compacted");
  expect(structured?.textContent).not.toContain("Structured record;");

  await toggleConversationOptions(view);
  const earlierCapture = findVersionMenuItem(view, "codex-version-1");
  await act(async () => {
    earlierCapture.click();
    await flush();
  });
  expect(view.textContent).toContain("This is the earlier captured version.");
  expect(view.textContent).not.toContain("I found two snapshots for this session.");
});

it("shows full import details for all machines and returns to the filtered session", async () => {
  const sessionPath = "/sessions/codex-thread-1?machine=thinkpad&harness=codex&version=codex-version-1";
  const view = await renderViewer(sessionFixtureApi, sessionPath);

  await act(async () => {
    findControl(view, "Import details").click();
    await flush();
  });
  expect(view.textContent).toContain("Import progress details");
  expect(view.textContent).toContain("Snapshots: 3 of 5 registered complete");
  expect(view.textContent).toContain("Codex metadata: 0 pending · 0 failed · 1 partial");
  expect(view.textContent).toContain("Complete fixture machine");
  expect(view.textContent).toContain("File counts cover entries inventoried so far");
  expect(findControl(view, "Back to sessions").getAttribute("href")).toBe(sessionPath);

  await act(async () => {
    findControl(view, "Back to sessions").click();
    await flush();
  });
  expect(view.textContent).toContain("Local agent sessions");
  expect(view.querySelector('input[aria-label="Filter by machine"]')).toHaveProperty("value", "ThinkPad");
  expect(view.textContent).toContain("This is the earlier captured version.");
});

it("keeps an un-inventoried snapshot pending without a zero-denominator progress bar", async () => {
  const [fixtureMachine] = await sessionFixtureApi.listImportProgress();
  if (fixtureMachine === undefined) throw new Error("The session fixture has no import progress");

  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listImportProgress() {
      return [
        {
          ...fixtureMachine,
          machine_id: "new-machine",
          display_name: "New machine",
          known_snapshot_count: 1,
          completed_snapshot_count: 0,
          failed_snapshot_count: 0,
          codex_metadata_pending_snapshot_count: 0,
          codex_metadata_failed_snapshot_count: 0,
          codex_metadata_partial_snapshot_count: 0,
          oldest_unfinished_snapshot_at: "2026-10-28T17:00:00Z",
          oldest_unfinished_snapshot_state: "inventory_pending",
          processed_file_count: 0,
          pending_file_count: 0,
          failed_file_count: 0,
        },
      ];
    },
  };
  const view = await renderViewer(api);

  expect(view.querySelector('[aria-label="Syncing New machine"]')).toBeNull();
  expect(view.textContent).toContain("1/1 snapshots remaining");
  const snapshotProgress = view.querySelector<HTMLElement>(
    '[role="progressbar"][aria-label="New machine snapshot progress"]'
  );
  expect(snapshotProgress?.getAttribute("aria-valuenow")).toBe("0");
  expect(snapshotProgress?.getAttribute("aria-valuemax")).toBe("1");
  expect(snapshotProgress?.classList.contains("session-import-machine-progress-active")).toBe(true);
  expect(snapshotProgress?.querySelector(".session-import-machine-progress-pending")).not.toBeNull();

  await act(async () => {
    findControl(view, "Import details").click();
    await flush();
  });
  expect(view.textContent).toContain("Snapshots: 0 of 1 registered complete");
  expect(view.textContent).toContain("Inventory pending");
  expect(view.textContent).toContain("No file entries inventoried yet.");
  expect(view.textContent).toContain("File entries: 0 processed · 0 pending · 0 failed");
  expect(view.querySelectorAll('[role="progressbar"]')).toHaveLength(0);
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
      session_kind: "main",
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
        <MemoryRouter initialEntries={["/?view=import-progress"]}>
          <App api={api} />
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
  let importProgressRequests = 0;
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listImportProgress(signal) {
      importProgressRequests += 1;
      return sessionFixtureApi.listImportProgress(signal);
    },
    listSessions(query, signal) {
      sessionListRequests += 1;
      return sessionFixtureApi.listSessions(query, signal);
    },
  };
  const view = await renderViewer(api);
  const requestsBeforeRefresh = sessionListRequests;
  const progressRequestsBeforeRefresh = importProgressRequests;

  await act(async () => {
    findControl(view, "Refresh session list").click();
    await flush();
  });

  expect(sessionListRequests).toBe(requestsBeforeRefresh + 1);
  expect(importProgressRequests).toBe(progressRequestsBeforeRefresh);
  expect(findControl(view, "Import details")).toBeDefined();
});

it("shows inferred subagent links and navigates to a placeholder parent", async () => {
  const view = await renderViewer(sessionFixtureApi, "/?kind=main&kind=subagent&kind=unknown");

  await act(async () => {
    findControl(view, "Inspect a local transcript").click();
    await flush();
  });
  const related = await openRelatedSessions(view);
  expect(related.textContent).toContain("Parent inferred from Claude subagent path");
  expect(related.textContent).toContain("Inferred Claude parent · session 12ab");

  await act(async () => {
    findControl(related, "Inferred Claude parent · session 12ab").click();
    await flush();
  });
  const parentRelated = await openRelatedSessions(view);
  expect(view.textContent).toContain("No transcript version is available. Inventory alone does not establish");
  expect(parentRelated.textContent).toContain("Inspect a local transcript");

  await act(async () => {
    findControl(parentRelated, "Inspect a local transcript").click();
    await flush();
  });
  const childRelated = await openRelatedSessions(view);
  expect(childRelated.textContent).toContain("Parent inferred from Claude subagent path");
  expect(view.textContent).toContain("Review this local conversation.");
});

it("shows unresolved native parent metadata without inventing a session link", async () => {
  const unresolvedSession: SessionSummary = {
    id: "codex-child-unresolved",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
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
  expect(view.querySelector<HTMLButtonElement>(".session-browser-related-trigger")?.getAttribute("aria-label")).toBe(
    "Related sessions (0 shown)"
  );
  const related = await openRelatedSessions(view);

  expect(related.textContent).toContain("Codex parent_thread_id is unresolved");
  expect(related.textContent).toContain("Native parent ID: thread-parent-123");
  expect(related.textContent).toContain("Parent session could not be resolved to a captured session.");
});

it("keeps native parent metadata reachable without counting an uncaptured parent relation", async () => {
  const metadataOnlySession: SessionSummary = {
    id: "codex-child-metadata-only",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
    title: "Inspect native metadata without a parent link",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: null,
    parent_link_kind: null,
    parent_native_session_id: "thread-parent-unlinked",
    parent_native_session_id_conflict: true,
    parent_native_session_id_candidates: ["thread-parent-unlinked", "thread-parent-candidate"],
    identity_inferred: false,
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query) {
      if (query.parentSessionId !== undefined) return Promise.resolve({ data: [], next_cursor: null });
      return Promise.resolve({ data: [metadataOnlySession], next_cursor: null });
    },
  };
  const view = await renderViewer(api, `/sessions/${metadataOnlySession.id}`);
  const toggle = view.querySelector<HTMLButtonElement>(".session-browser-related-trigger");
  expect(toggle?.getAttribute("aria-label")).toBe("Related sessions (0 shown)");
  const related = await openRelatedSessions(view);

  expect(related.textContent).toContain("Native parent ID: thread-parent-unlinked");
  expect(related.textContent).toContain(
    "Conflicting native parent ID candidates: thread-parent-unlinked, thread-parent-candidate"
  );
  expect(related.textContent).toContain("Parent session could not be resolved to a captured session.");
  expect(related.querySelector("a")).toBeNull();
});

it("shows parent and child lookup failures instead of leaving loading indicators active", async () => {
  const session: SessionSummary = {
    id: "codex-child-unavailable-parent",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
    title: "Inspect a child with unavailable relations",
    cwd: null,
    started_at: null,
    last_activity_at: null,
    latest_version_id: null,
    version_count: 0,
    latest_version_record_count: null,
    transcript_status: "unavailable",
    parent_session_id: "missing-parent",
    parent_link_kind: "codex_parent_thread_id",
    parent_native_session_id: "thread-parent-missing",
    identity_inferred: false,
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listSessions(query) {
      if (query.parentSessionId !== undefined) return Promise.reject(new Error("child lookup failed"));
      return Promise.resolve({ data: [session], next_cursor: null });
    },
    getSession(sessionId) {
      if (sessionId === "missing-parent") return Promise.reject(new Error("parent lookup failed"));
      return sessionFixtureApi.getSession(sessionId);
    },
  };
  const view = await renderViewer(api, `/sessions/${session.id}`);
  const related = await openRelatedSessions(view);

  expect(related.textContent).toContain("Could not load parent session: parent lookup failed");
  expect(related.textContent).toContain("Could not load child sessions: child lookup failed");
  expect(related.querySelector('[aria-label="Loading parent session"]')).toBeNull();
  expect(related.querySelector('[aria-label="Loading child sessions"]')).toBeNull();
});

it("shows conflicting Codex native and parent IDs without inventing a parent link", async () => {
  const conflictingSession: SessionSummary = {
    id: "codex-child-conflicting",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
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
  const related = await openRelatedSessions(view);

  expect(view.textContent).toContain("Conflicting native session IDs");
  expect(view.textContent).toContain("Native session ID candidates: thread-child-a, thread-child-b");
  expect(related.textContent).toContain("Conflicting Codex parent_thread_id values");
  expect(related.textContent).toContain("Conflicting native parent ID candidates: thread-parent-a, thread-parent-b");
  expect(related.textContent).toContain("Parent session could not be resolved to a captured session.");
});

it("labels a resolved Codex parent_thread_id link and navigates to the captured parent", async () => {
  const parent: SessionSummary = {
    id: "codex-parent-session",
    machine_id: "thinkpad",
    harness: "codex",
    session_kind: "main",
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
      if (sessionId === child.id) return Promise.resolve(child);
      if (sessionId === parent.id) return Promise.resolve(parent);
      throw new Error(`Unknown fixture session ${sessionId}`);
    },
  };
  const view = await renderViewer(api);
  const related = await openRelatedSessions(view);

  expect(related.textContent).toContain("Parent linked from Codex parent_thread_id");
  expect(related.textContent).toContain("Native parent ID: thread-parent-123");
  await act(async () => {
    findControl(related, "Captured Codex parent").click();
    await flush();
  });
  expect(view.textContent).toContain("Captured Codex parent");
  expect(view.querySelector(".session-browser-related-trigger")).toBeNull();
});

it("does not prepend an older event page after switching sessions", async () => {
  const pending = deferred<EventPage<EventRecord>>();
  const eventRequests: Array<[string, string]> = [];
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listEvents(sessionId, versionId, cursor, signal) {
      eventRequests.push([sessionId, versionId]);
      if (sessionId === "codex-thread-1" && cursor === 2) return pending.promise;
      return sessionFixtureApi.listEvents(sessionId, versionId, cursor, signal);
    },
  };
  const view = await renderViewer(api, "/?kind=main&kind=subagent&kind=unknown");

  await scrollToEarlierRecords(view);
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

it.each(["harness", "kind"])("does not append a session page after changing the %s filter", async (filter) => {
  const pending = deferred<SessionPage<SessionSummary>>();
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listSessions(query, signal) {
      if (query.cursor !== undefined) return pending.promise;
      const page = await sessionFixtureApi.listSessions(query, signal);
      return { data: page.data.slice(0, 1), next_cursor: query.harness === undefined ? "older" : null };
    },
  };
  const view = await renderViewer(api, "/?kind=main&kind=subagent&kind=unknown");

  await act(async () => {
    findControl(view, "Load more sessions").click();
    await Promise.resolve();
  });
  if (filter === "harness") {
    const harness = view.querySelector<HTMLSelectElement>('select[aria-label="Filter by harness"]');
    expect(harness).not.toBeNull();
    await act(async () => {
      harness!.value = "claude";
      harness!.dispatchEvent(new Event("change", { bubbles: true }));
      await flush();
    });
  } else {
    await toggleKind(view, "main");
    await toggleKind(view, "unknown");
  }
  expect(view.textContent).toContain("Inspect a local transcript");

  pending.resolve({
    data: [
      {
        id: "stale-session",
        machine_id: "thinkpad",
        harness: "codex",
        session_kind: "main",
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

it("keeps commentary visible and pairs a tool's arguments, output and source records", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/mixed-transcript-fixture");
  const tool = view.querySelector<HTMLElement>('[data-activity-step="tool"]')!;
  const group = findControl(tool, "read_build_log");
  expect(group.getAttribute("aria-expanded")).toBe("false");
  expect(view.textContent).toContain("missing test configuration");
  expect(view.textContent).toContain("I will inspect the build log.");
  expect(view.textContent).not.toContain("Fixture build output");
  await act(async () => group.click());
  expect(tool.textContent).toContain("read_build_log");
  expect(tool.textContent).toContain("Arguments");
  expect(tool.textContent).toContain("Fixture build output");
  const rawControl = findControl(tool, "Raw JSONL record");
  await act(async () => rawControl.click());
  const rawPanel = document.getElementById(rawControl.getAttribute("aria-controls")!)!;
  expect(rawPanel.querySelectorAll('[role="region"][aria-label="Raw JSONL record"]')).toHaveLength(3);
  expect(rawPanel.textContent).toContain('"call_id": "build-log"');
  expect(rawPanel.textContent).toContain("function_call_output");
});

it("retains an open result step when prepend supplies its matching call", async () => {
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listEvents(sessionId, versionId, cursor, signal) {
      const page = await sessionFixtureApi.listEvents(sessionId, versionId, undefined, signal);
      return cursor === undefined
        ? { data: page.data.filter((record) => record.ordinal >= 6), next_cursor: 6 }
        : { data: page.data.filter((record) => record.ordinal < cursor), next_cursor: null };
    },
  };
  const view = await renderViewer(api, "/sessions/mixed-transcript-fixture");
  await act(async () =>
    findControl(view.querySelector<HTMLElement>('[data-activity-step="tool"]')!, "Tool result").click()
  );
  expect(view.textContent).toContain("The call is outside the loaded records");
  await scrollToEarlierRecords(view);
  expect(
    findControl(view.querySelector<HTMLElement>('[data-activity-step="tool"]')!, "read_build_log").getAttribute(
      "aria-expanded"
    )
  ).toBe("true");
  expect(view.textContent).toContain("Arguments");
  expect(view.textContent).toContain("Fixture build output");
  expect(view.textContent).not.toContain("The call is outside the loaded records");
});

it("renders session-shaped Codex work without bookkeeping or unavailable reasoning placeholders", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/codex-work-fixture");
  expect(view.textContent).toContain("The components are shared.");
  expect(view.textContent).toContain("Context compacted");
  expect(view.textContent).not.toContain("Structured record;");
  expect(view.querySelectorAll("[data-activity-step]")).toHaveLength(0);
  expect(view.textContent).not.toContain("item completed");
  await act(async () => findControl(view, "1 tool call, 2 reasoning steps").click());
  expect(view.querySelectorAll('[data-activity-step="reasoning"]')).toHaveLength(1);
  const reasoning = view.querySelector<HTMLElement>('[data-activity-step="reasoning"]')!;
  await act(async () => findControl(reasoning, "Reasoning").click());
  expect(reasoning.textContent).toContain("Pair tool calls by their recorded call ID.");
  expect(reasoning.textContent).toContain("Keep commentary outside the work group");
});

it.each(["encrypted", "unknown"])("distinguishes %s agent messages without guessing at body fields", async (kind) => {
  const parsed = {
    type: "response_item",
    payload: {
      type: "agent_message",
      author: "/root/reviewer",
      recipient: "/root",
      ...(kind === "encrypted"
        ? {
            content: [
              {
                type: "input_text",
                text: "Message Type: MESSAGE\nTask name: /root\nSender: /root/reviewer\nPayload:\n",
              },
              { type: "encrypted_content", encrypted_content: "ciphertext" },
            ],
          }
        : { message: "Do not guess that this is the body." }),
    },
  };
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listEvents() {
      return {
        data: [
          { ordinal: 1, parsed, kind: "agent_message", timestamp: null, raw_base64: btoa(JSON.stringify(parsed)) },
        ],
        next_cursor: null,
      };
    },
  };
  const view = await renderViewer(api);
  const step = view.querySelector<HTMLElement>(
    `[data-activity-step="${kind === "encrypted" ? "subagent" : "record"}"]`
  )!;
  expect(step).not.toBeNull();
  await act(async () => findControl(step, kind === "encrypted" ? "Agent message" : "agent message").click());
  expect(view.textContent).not.toContain("Message content unavailable");
  if (kind === "encrypted") {
    expect(step.textContent).toContain("Message body is encrypted in the archive.");
    expect(step.textContent).not.toContain("ciphertext");
    expect(step.textContent).not.toContain("Payload:");
  } else {
    expect(step.textContent).not.toContain("Do not guess that this is the body.");
    await act(async () => findControl(step, "Raw JSONL record").click());
    expect(view.textContent).toContain("Do not guess that this is the body.");
  }
});

it("keeps records without readable content compact and inspectable", async () => {
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listEvents() {
      return {
        data: [
          { ordinal: 1, parsed: null, kind: "Null record", timestamp: null, raw_base64: btoa("null") },
          {
            ordinal: 2,
            parsed: { type: "future_record", opaque: [1, 2] },
            kind: null,
            timestamp: null,
            raw_base64: btoa('{"type":"future_record","opaque":[1,2]}'),
          },
        ],
        next_cursor: null,
      };
    },
  };
  const view = await renderViewer(api);
  expect(view.textContent).not.toContain("Structured record;");
  for (const [ordinal, label, raw] of [
    [1, "Null record", "null"],
    [2, "future_record", '{"type":"future_record","opaque":[1,2]}'],
  ] as const) {
    const record = view.querySelector<HTMLElement>(`[data-event-ordinal="${ordinal}"]`)!;
    expect(record.textContent).toContain(label);
    const inspect = findControl(record, "Raw JSONL record");
    await act(async () => inspect.click());
    expect(JSON.parse(record.querySelector(".cm-content")!.textContent!)).toEqual(JSON.parse(raw));
  }
});

it("renders Claude attachments as individually expandable widgets", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/claude-attachments-fixture");
  expect(view.textContent).not.toContain("fixture hook completed");
  await act(async () => findControl(view, "2 attachments").click());
  const attachments = view.querySelectorAll<HTMLElement>('[data-activity-step="attachment"]');
  expect(attachments).toHaveLength(2);
  expect(attachments[0].textContent).toContain("Attachment · hook success");
  expect(attachments[1].textContent).toContain("/fixture/config.ts");
  expect(view.textContent).not.toContain("export const enabled");
  await act(async () => findControl(attachments[1], "Attachment · edited text file").click());
  expect(attachments[1].textContent).toContain("export const enabled = true;");
  await act(async () => findControl(attachments[0], "Attachment · hook success").click());
  expect(attachments[0].textContent).toContain("fixture hook completed");
  await act(async () => findControl(attachments[1], "Raw JSONL record").click());
  expect(attachments[1].querySelector(".archive-raw-record .cm-content")?.textContent).toContain(
    '"type": "attachment"'
  );
});

it("pairs multiple Claude tool blocks and leaves mixed-message prose visible", async () => {
  const view = await renderViewer(sessionFixtureApi, "/sessions/claude-work-fixture");
  expect(view.textContent).toContain("The captured check failed. I'll inspect its configuration.");
  expect(view.textContent).toContain("Use the captured data only.");
  expect(view.textContent).not.toContain("The captured test could not find");
  await act(async () => findControl(view, "2 tool calls, 1 reasoning step, 1 attachment").click());
  const tools = view.querySelectorAll<HTMLElement>('[data-activity-step="tool"]');
  expect(tools).toHaveLength(3);
  const read = [...tools].find((tool) => tool.textContent?.includes("README.md"))!;
  const failed = [...tools].find((tool) => tool.textContent?.includes("check-fixture"))!;
  await act(async () => findControl(read, "Read").click());
  expect(read.textContent).toContain("The fixture README documents the required setting.");
  expect(failed.textContent).not.toContain("The captured test could not find");
  await act(async () => findControl(failed, "Bash").click());
  expect(failed.textContent).toContain("The captured test could not find the required setting.");
  await act(async () => findControl(read, "Raw JSONL record").click());
  const raw = document.getElementById(findControl(read, "Raw JSONL record").getAttribute("aria-controls")!)!;
  expect(raw.querySelectorAll('[role="region"][aria-label="Raw JSONL record"]')).toHaveLength(2);
  expect(raw.textContent).toContain('"id": "read-doc"');
  expect(raw.textContent).toContain('"tool_use_id": "read-doc"');
});

it("retains the selected Claude result block when prepend supplies multiple calls", async () => {
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    async listEvents(sessionId, versionId, cursor, signal) {
      const page = await sessionFixtureApi.listEvents(sessionId, versionId, undefined, signal);
      return cursor === undefined
        ? { data: page.data.filter((record) => record.ordinal >= 5), next_cursor: 5 }
        : { data: page.data.filter((record) => record.ordinal < cursor), next_cursor: null };
    },
  };
  const view = await renderViewer(api, "/sessions/claude-work-fixture");
  await act(async () => findControl(view, "2 tool results").click());
  const result = [...view.querySelectorAll<HTMLElement>('[data-activity-step="tool"]')].find((tool) =>
    tool.textContent?.includes("README")
  )!;
  await act(async () => findControl(result, "Tool result").click());
  expect(result.textContent).toContain("The call is outside the loaded records");
  await scrollToEarlierRecords(view);
  const paired = [...view.querySelectorAll<HTMLElement>('[data-activity-step="tool"]')].find((tool) =>
    tool.textContent?.includes("README.md")
  )!;
  expect(findControl(paired, "Read").getAttribute("aria-expanded")).toBe("true");
  expect(paired.textContent).toContain("The fixture README documents the required setting.");
  expect(paired.textContent).not.toContain("The call is outside the loaded records");
});
