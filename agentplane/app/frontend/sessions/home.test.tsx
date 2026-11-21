import type * as ThreadClientModule from "./client";
// @vitest-environment happy-dom
import { toJson } from "@bufbuild/protobuf";
import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { readySandbox } from "../testing/sandbox";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { listThreadMetadata } from "./client";
import { SessionSchema } from "../../../session_dispatcher/dispatcher/api/session/session_dispatcher_pb";
import { SessionSummarySchema } from "../../../history_service/api/history/history_service_pb";
import { Home } from "./home";
import { RoutedThreadsLiveProvider } from "./live";
import { SandboxesLiveProvider } from "../sandboxes/live";
import { Sidebar } from "../shell/sidebar";
import { ServedWatch, serveEventStreams } from "../testing/watch";
import { threadView } from "./testing/state";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";

vi.mock("./client", async (importOriginal) => ({
  ...(await importOriginal<typeof ThreadClientModule>()),
  listThreadMetadata: vi.fn(),
}));

let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const SANDBOX = readySandbox("test-sandbox", "00000000-0000-4000-8000-000000000001");
SANDBOX.kubernetesDiagnostics!.pod = undefined;

const THREADS = [
  threadView({
    metadata: { threadId: "t-1", name: "Alpha thread" },
    summary: {
      sandbox: { name: SANDBOX.target!.name, uid: SANDBOX.target!.uid },
      lastEventAt: timestampFromDate(new Date("2026-01-01T11:58:00Z")),
    },
  }),
  threadView({
    metadata: { threadId: "t-2", name: "Beta thread" },
    summary: { sandbox: { name: SANDBOX.target!.name, uid: SANDBOX.target!.uid } },
  }),
];

/** Renders Home beside the Sidebar, the way the shell lays them out on desktop; returns the URL of
 * every threads stream opened, in order. */
async function render(): Promise<string[]> {
  const threadStreams: string[] = [];
  const otherFetch = globalThis.fetch;
  serveEventStreams(
    (stream) => {
      const { pathname, searchParams } = stream.url;
      if (/^\/upstream\/notifications\/v1\/sessions\/[^/]+\/inbox:watch$/.test(pathname)) {
        stream.send({ noInbox: {} });
        return;
      }
      if (pathname === "/upstream/environments/v1/environments:watch") {
        stream.send({ snapshot: { environments: [toJson(EnvironmentDescriptorSchema, SANDBOX)] } });
        stream.send({ snapshotComplete: {} });
        return;
      }
      throw new Error(`Unexpected event stream route: ${pathname}${searchParams}`);
    },
    otherFetch,
    (path) =>
      /^\/upstream\/notifications\/v1\/sessions\/[^/]+\/inbox:watch$/.test(path) ||
      path === "/upstream/environments/v1/environments:watch"
  );
  const eventFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (new Headers(init?.headers).get("Accept") === "application/x-ndjson") {
      const watch = new ServedWatch(String(input), init?.signal ?? undefined);
      const pathname = watch.url.pathname;
      if (
        pathname === "/upstream/environments/v1/environments:watch" ||
        pathname.startsWith("/upstream/notifications/")
      )
        return eventFetch(input, init);
      if (
        pathname !== "/upstream/dispatcher/v1/sessions:watch" &&
        pathname !== "/upstream/app/v1/threads:watch" &&
        pathname !== "/upstream/history/v1/session-summaries:watchSelected"
      ) {
        throw new Error(`Unexpected Thread watch route: ${pathname}`);
      }
      threadStreams.push(pathname);
      if (pathname === "/upstream/dispatcher/v1/sessions:watch") {
        for (const candidate of THREADS)
          if (candidate.session)
            watch.send({ session: toJson(SessionSchema, candidate.session), resumeToken: "test-session" });
        watch.send({ caughtUp: {}, resumeToken: "test-snapshot-token" });
      } else if (pathname === "/upstream/app/v1/threads:watch") {
        watch.send({ changed: {}, updatesConnected: true });
      } else {
        const ids = watch.url.searchParams.getAll("session_ids");
        watch.send({
          snapshot: {
            summaries: THREADS.filter((row) => ids.includes(row.metadata.threadId)).map((row) =>
              toJson(SessionSummarySchema, row.summary)
            ),
          },
        });
      }
      return Promise.resolve(watch.response);
    }
    return eventFetch(input, init);
  });
  vi.mocked(listThreadMetadata).mockImplementation(
    async () => new Map(THREADS.map((row) => [row.metadata.threadId, row.metadata]))
  );
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <RoutedThreadsLiveProvider>
            <SandboxesLiveProvider>
              <Sidebar settingsOpen={false} onOpenSettings={() => undefined} open onClose={() => undefined} />
              <Home />
            </SandboxesLiveProvider>
          </RoutedThreadsLiveProvider>
        </MemoryRouter>
      </MantineProvider>
    )
  );
  return threadStreams;
}

function home(): HTMLElement {
  const found = container.querySelector<HTMLElement>('section[aria-label="Sandboxes and threads"]');
  if (!found) throw new Error("missing Home");
  return found;
}

function sidebar(): HTMLElement {
  const found = container.querySelector<HTMLElement>("nav.agentplane-sidebar");
  if (!found) throw new Error("missing sidebar");
  return found;
}

function row(scope: HTMLElement, name: string): HTMLElement {
  const found = [...scope.querySelectorAll<HTMLElement>(".agentplane-thread-list-row")].find((candidate) =>
    candidate.textContent?.includes(name)
  );
  if (!found) throw new Error(`missing row ${name}`);
  return found;
}

function searchBox(scope: HTMLElement): HTMLInputElement {
  const found = scope.querySelector('input[aria-label="Search threads"]');
  if (!(found instanceof HTMLInputElement)) throw new Error("missing search box");
  return found;
}

it("adds each Thread's harness, model and last activity to its row, which the sidebar leaves out", async () => {
  await render();
  const alpha = row(home(), "Alpha thread").querySelector(".agentplane-thread-list-row-detail");
  expect(alpha?.textContent).toContain("Claude · test-model");
  expect(alpha?.querySelector("time")?.dateTime).toBe("2026-01-01T11:58:00.000Z");
  expect(row(home(), "Beta thread").querySelector(".agentplane-thread-list-row-detail time")).toBeNull();
  expect(sidebar().querySelector(".agentplane-thread-list-row-detail")).toBeNull();
});

it("shares one search with the sidebar while reusing the generated update watches", async () => {
  const threadStreams = await render();
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const input = searchBox(home());
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "beta");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(searchBox(sidebar()).value).toBe("beta");
  await act(async () => vi.runAllTimers());
  expect(threadStreams.filter((path) => path === "/upstream/dispatcher/v1/sessions:watch")).toHaveLength(1);
  expect(threadStreams.filter((path) => path === "/upstream/app/v1/threads:watch")).toHaveLength(1);
  expect(threadStreams.filter((path) => path === "/upstream/history/v1/session-summaries:watchSelected")).toHaveLength(
    2
  );
  expect(listThreadMetadata).toHaveBeenCalledTimes(1);
  for (const scope of [home(), sidebar()]) {
    expect(row(scope, "Beta thread")).toBeDefined();
    expect(scope.textContent).not.toContain("Alpha thread");
  }
});
