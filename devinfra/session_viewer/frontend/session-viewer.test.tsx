// @vitest-environment happy-dom

import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it } from "vitest";

import type { SessionBrowserApi } from "./api";
import { SessionViewer } from "./session-viewer";
import { sessionFixtureApi } from "./fixtures/session-data";
import type { EventPage, EventRecord, SessionPage, SessionSummary } from "./schema";

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

let root: ReturnType<typeof createRoot> | null = null;
let container: HTMLDivElement | null = null;

afterEach(async () => {
  if (root !== null) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

async function renderViewer(api: SessionBrowserApi): Promise<HTMLDivElement> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MantineProvider env="test">
        <SessionViewer api={api} />
      </MantineProvider>
    );
    await flush();
  });
  return container;
}

async function flush(): Promise<void> {
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
}

function findButton(view: HTMLElement, text: string): HTMLButtonElement {
  const button = [...view.querySelectorAll<HTMLButtonElement>("button")].find((item) =>
    item.textContent?.includes(text)
  );
  if (button === undefined) throw new Error(`Could not find button containing ${JSON.stringify(text)}`);
  return button;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

it("shows captured machines, readable records, malformed raw lines, and older versions", async () => {
  const view = await renderViewer(sessionFixtureApi);

  expect(view.textContent).toContain("ThinkPad");
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
    findButton(view, "Load more records").click();
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

it("shows inferred subagent links and navigates to a placeholder parent", async () => {
  const view = await renderViewer(sessionFixtureApi);

  await act(async () => {
    findButton(view, "Inspect a local transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("Parent inferred from Claude subagent path");
  expect(view.textContent).toContain("Inferred Claude parent · session 12ab");

  await act(async () => {
    findButton(view, "Inferred Claude parent · session 12ab").click();
    await flush();
  });
  expect(view.textContent).toContain("This path-derived placeholder has no captured transcript yet.");
  expect(view.textContent).toContain("Path-inferred link");
  expect(view.textContent).toContain("Inspect a local transcript");

  await act(async () => {
    findButton(view, "Inspect a local transcript").click();
    await flush();
  });
  expect(view.textContent).toContain("Review this local conversation.");
});

it("does not append an event page after switching sessions", async () => {
  const pending = deferred<EventPage<EventRecord>>();
  const api: SessionBrowserApi = {
    ...sessionFixtureApi,
    listEvents(sessionId, versionId, cursor, signal) {
      if (sessionId === "codex-thread-1" && cursor === 3) return pending.promise;
      return sessionFixtureApi.listEvents(sessionId, versionId, cursor, signal);
    },
  };
  const view = await renderViewer(api);

  await act(async () => {
    findButton(view, "Load more records").click();
    await Promise.resolve();
  });
  await act(async () => {
    findButton(view, "Inspect a local transcript").click();
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
    findButton(view, "Load more sessions").click();
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
        parent_session_id: null,
        parent_link_kind: null,
        identity_inferred: false,
      },
    ],
    next_cursor: null,
  });
  await act(async () => flush());

  expect(view.textContent).not.toContain("Stale session page");
  expect(view.textContent).toContain("Inspect a local transcript");
});
