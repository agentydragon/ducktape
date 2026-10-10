// @vitest-environment happy-dom

import { type JSX, act } from "react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ActionRequestDetail } from "./detail";
import { actionGroupService, actionService } from "./client";
import { ActionsSidebarSection } from "./sidebar";
import { ActionRequestsProvider } from "./requests";
import { mount, request, unmountLast } from "./testing";

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
  const get = vi.spyOn(actionService, "get");
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>('a[aria-label^="View details for ssh / exec"]');
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(get).not.toHaveBeenCalled();
  expect(container.textContent).toContain("Exact arguments (unredacted)");
  expect(container.textContent).toContain("exact-command");

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread");
  expect(container.textContent).toContain("Thread contents remain open");
});

it.each([
  { verdict: "allow" as const, resultState: "allowed" as const, buttonName: "Approve" },
  { verdict: "deny" as const, resultState: "denied" as const, buttonName: "Deny" },
])(
  "returns to the originating page after a successful $verdict decision",
  async ({ verdict, resultState, buttonName }) => {
    vi.spyOn(actionGroupService, "list").mockResolvedValue([]);
    const decide = vi.spyOn(actionService, "decide").mockImplementation(async (row) => ({
      ...row,
      ...request(resultState, 21),
      action: ROW.action,
      arguments: ROW.arguments,
      title: ROW.title,
    }));
    const container = await mount(<App initialPath="/threads/example-thread" />);
    await send([ROW]);

    const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
    if (!disclosure) throw new Error("missing pending action disclosure");
    await act(async () => disclosure.click());
    const detailsLink = container.querySelector<HTMLAnchorElement>('a[aria-label^="View details for ssh / exec"]');
    if (!detailsLink) throw new Error("missing action details link");
    await act(async () => detailsLink.click());

    const decisionButton = container.querySelector<HTMLButtonElement>(`button[aria-label="${buttonName}"]`);
    if (!decisionButton) throw new Error(`missing ${buttonName} button`);
    await act(async () => decisionButton.click());
    expect(decide).toHaveBeenCalledWith(ROW, verdict);
    await vi.waitFor(() =>
      expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread")
    );
    expect(container.textContent).toContain("Thread contents remain open");
  }
);

it("stays on action details when a decision fails", async () => {
  vi.spyOn(actionService, "decide").mockRejectedValue(new Error("decision was rejected"));
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>('a[aria-label^="View details for ssh / exec"]');
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
  vi.spyOn(actionGroupService, "list").mockResolvedValue([]);
  vi.spyOn(actionService, "get").mockResolvedValue(ROW);
  vi.spyOn(actionService, "decide").mockResolvedValue({
    ...request("allowed", 21),
    action: ROW.action,
    arguments: ROW.arguments,
    title: ROW.title,
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
  const get = vi.spyOn(actionService, "get").mockResolvedValue(ROW);
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} />);
  await send([]);
  expect(get).toHaveBeenCalledWith(ROW.id);
  expect(container.textContent).toContain("Exact arguments (unredacted)");

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/actions");
  expect(container.textContent).toContain("All actions");
});

it("refreshes the durable receipt when SSE removes an action that was pending", async () => {
  vi.spyOn(actionGroupService, "list").mockResolvedValue([]);
  const allowed = {
    ...request("allowed", 21),
    id: ROW.id,
    title: ROW.title,
    action: ROW.action,
    arguments: ROW.arguments,
  };
  const get = vi.spyOn(actionService, "get").mockResolvedValue(allowed);
  const container = await mount(<App initialPath="/threads/example-thread" />);
  await send([ROW]);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>('a[aria-label^="View details for ssh / exec"]');
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());
  expect(get).not.toHaveBeenCalled();

  await send([]);
  expect(get).toHaveBeenCalledWith(ROW.id);
  expect(container.querySelector('button[aria-label="Approve"]')).toBeNull();
  expect(container.querySelector('button[aria-label="Deny"]')).toBeNull();
});

afterEach(async () => {
  await unmountLast();
  sessionStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
