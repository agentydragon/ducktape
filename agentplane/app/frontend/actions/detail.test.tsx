// @vitest-environment happy-dom

import { type JSX, act } from "react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ActionRequestDetail } from "./detail";
import type { ActionRequestView } from "./client";
import { ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { answerApi, mount, request, unmountLast, type Sent } from "./testing";

const ROW = {
  ...request("decision_pending", 21),
  action: { group: "ssh", name: "exec" },
  arguments: { host: "test-host.example", user: "test-user", command: "echo exact-command" },
};
const stream: { current?: EventTarget } = {};

class ActionStream extends EventTarget {
  close = vi.fn();

  constructor(url: string) {
    super();
    if (url !== "/actions/stream?state=decision_pending") throw new Error(`unexpected Action stream URL: ${url}`);
    stream.current = this;
  }
}

async function send(rows: (typeof ROW)[]): Promise<void> {
  await act(async () => {
    stream.current?.dispatchEvent(new MessageEvent("snapshot", { data: JSON.stringify(rows) }));
  });
}

/** Answers the detail page's reads: `stored` for the request itself, `decided` for a decision. */
function serve({ stored, decided }: { stored?: ActionRequestView; decided?: ActionRequestView | Response }): Sent[] {
  return answerApi(({ method, path }) => {
    if (path === "/action-groups") return [];
    if (method === "GET" && path === `/actions/${ROW.id}`) return stored;
    if (method === "POST" && path === `/actions/${ROW.id}/decision`) return decided;
  });
}

const fetched = (sent: Sent[]): boolean =>
  sent.some(({ method, path }) => method === "GET" && path === `/actions/${ROW.id}`);

function DetailRoute(): JSX.Element {
  const { requestId } = useParams();
  if (requestId === undefined) throw new Error("missing requestId");
  return <ActionRequestDetail requestId={requestId} />;
}

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function App({ initialPath }: { initialPath: string }): JSX.Element {
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <ActionRequestsProvider>
        <ActionsSidebarSection />
        <CurrentPath />
        <Routes>
          <Route path="/threads/:threadId" element={<div>Thread contents remain open</div>} />
          <Route path="/actions/:requestId" element={<DetailRoute />} />
          <Route path="/actions" element={<div>All actions</div>} />
        </Routes>
      </ActionRequestsProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  sessionStorage.clear();
  stream.current = undefined;
  vi.stubGlobal("EventSource", ActionStream);
});

it("opens cached stream details without a reload and Back returns to the original in-app route", async () => {
  const sent = serve({});
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${ROW.title}"]`);
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(fetched(sent)).toBe(false);
  expect(container.textContent).toContain("Exact arguments (unredacted)");
  expect(container.textContent).toContain("exact-command");

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread");
  expect(container.textContent).toContain("Thread contents remain open");
});

it("renders an Action's human-facing label on the full details page", async () => {
  const row = {
    ...ROW,
    action: { group: "kubernetes_admin", name: "pods_list_in_namespace" },
    arguments: { namespace: "tofu-controller" },
  };
  serve({ stored: row });
  const container = await mount(<App initialPath={`/actions/${row.id}`} />);

  await vi.waitFor(() => expect(container.textContent).toContain("List pods in namespace tofu-controller"));
  expect(container.textContent).not.toContain("kubernetes_admin / pods_list_in_namespace");
});

it.each([
  { verdict: "allow" as const, resultState: "allowed" as const, buttonName: "Approve" },
  { verdict: "deny" as const, resultState: "denied" as const, buttonName: "Deny" },
])(
  "returns to the originating page after a successful $verdict decision",
  async ({ verdict, resultState, buttonName }) => {
    const sent = serve({
      decided: { ...ROW, ...request(resultState, 21), action: ROW.action, arguments: ROW.arguments, title: ROW.title },
    });
    const container = await mount(<App initialPath="/threads/example-thread" />);
    await send([ROW]);

    const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
    if (!disclosure) throw new Error("missing pending action disclosure");
    await act(async () => disclosure.click());
    const detailsLink = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${ROW.title}"]`);
    if (!detailsLink) throw new Error("missing action details link");
    await act(async () => detailsLink.click());

    const decisionButton = container.querySelector<HTMLButtonElement>(`button[aria-label="${buttonName}"]`);
    if (!decisionButton) throw new Error(`missing ${buttonName} button`);
    await act(async () => decisionButton.click());
    expect(sent).toContainEqual(
      expect.objectContaining({
        method: "POST",
        body: expect.objectContaining({ verdict, expected_version: ROW.version }),
      })
    );
    await vi.waitFor(() =>
      expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread")
    );
    expect(container.textContent).toContain("Thread contents remain open");
  }
);

it("stays on action details when a decision fails", async () => {
  serve({ decided: Response.json({ detail: "decision was rejected" }, { status: 409 }) });
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${ROW.title}"]`);
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());
  const approve = container.querySelector<HTMLButtonElement>('button[aria-label="Approve"]');
  if (!approve) throw new Error("missing Approve button");

  await act(async () => {
    approve.click();
    await vi.waitFor(() => expect(container.textContent).toContain("decision was rejected"));
  });

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(container.textContent).toContain("Exact arguments (unredacted)");
});

it("keeps a direct action link on its terminal receipt after a successful decision", async () => {
  serve({
    stored: ROW,
    decided: { ...request("allowed", 21), action: ROW.action, arguments: ROW.arguments, title: ROW.title },
  });
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  const approve = await vi.waitFor(() => {
    const button = container.querySelector<HTMLButtonElement>('button[aria-label="Approve"]');
    if (!button) throw new Error("missing Approve button");
    return button;
  });

  await act(async () => approve.click());
  await vi.waitFor(() => expect(container.textContent).toContain("Allowed"));

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(container.textContent).toContain("Action details");
});

it("loads uncached deep links and falls back to the Actions page on Back", async () => {
  const sent = serve({ stored: ROW });
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  await send([]);
  await vi.waitFor(() => expect(fetched(sent)).toBe(true));
  await vi.waitFor(() => expect(container.textContent).toContain("Exact arguments (unredacted)"));

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/actions");
  expect(container.textContent).toContain("All actions");
});

it("refreshes the durable receipt when SSE removes an action that was pending", async () => {
  const allowed = {
    ...request("allowed", 21),
    id: ROW.id,
    title: ROW.title,
    action: ROW.action,
    arguments: ROW.arguments,
  };
  const sent = serve({ stored: allowed });
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>(`a[aria-label="View details for ${ROW.title}"]`);
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());
  expect(fetched(sent)).toBe(false);

  await send([]);
  await vi.waitFor(() => expect(fetched(sent)).toBe(true));
  await vi.waitFor(() => expect(container.querySelector('button[aria-label="Approve"]')).toBeNull());
  expect(container.querySelector('button[aria-label="Deny"]')).toBeNull();
});

afterEach(async () => {
  await unmountLast();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
