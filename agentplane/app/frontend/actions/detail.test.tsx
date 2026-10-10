// @vitest-environment happy-dom

import { type JSX, act } from "react";
import { MemoryRouter, Route, Routes, useParams, useLocation } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ActionRequestDetail } from "./detail";
import { ActionRequestsContext } from "./requests";
import type { ActionRequestView, ActionService } from "./client";
import { ActionsSidebarSection } from "./affordance";
import { mount, request, unmountLast } from "./testing";

const ROW = {
  ...request("decision_pending", 21),
  action: { group: "ssh", name: "exec" },
  arguments: { host: "test-host.example", user: "test-user", command: "echo exact-command" },
};

function DetailRoute({ service }: { service: ActionService }): JSX.Element {
  const { requestId } = useParams();
  if (requestId === undefined) throw new Error("missing requestId");
  return <ActionRequestDetail requestId={requestId} service={service} />;
}

function CurrentPath(): JSX.Element {
  const location = useLocation();
  return <div data-testid="current-path">{location.pathname}</div>;
}

function App({
  initialPath,
  service,
  requests,
}: {
  initialPath: string;
  service: ActionService;
  requests: ActionRequestView[];
}): JSX.Element {
  const actions = {
    requests,
    error: null,
    loading: false,
    stream: null,
    deciding: null,
    decide: vi.fn(),
  };
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <ActionRequestsContext.Provider value={actions}>
        <ActionsSidebarSection />
        <CurrentPath />
        <Routes>
          <Route path="/threads/:threadId" element={<div>Thread contents remain open</div>} />
          <Route path="/actions/:requestId" element={<DetailRoute service={service} />} />
          <Route path="/actions" element={<div>All actions</div>} />
        </Routes>
      </ActionRequestsContext.Provider>
    </MemoryRouter>
  );
}

beforeEach(() => sessionStorage.clear());

it("opens full details from the sidebar and Back returns to the original in-app route", async () => {
  const service: ActionService = {
    list: vi.fn(async () => [ROW]),
    get: vi.fn(async () => ROW),
    decide: vi.fn(async (item) => item),
  };
  const container = await mount(<App initialPath="/threads/example-thread" service={service} requests={[ROW]} />);

  const disclosure = container.querySelector<HTMLButtonElement>(`button[aria-label="Expand ${ROW.title}"]`);
  if (!disclosure) throw new Error("missing pending action disclosure");
  await act(async () => disclosure.click());
  const detailsLink = container.querySelector<HTMLAnchorElement>('a[aria-label^="View details for ssh / exec"]');
  if (!detailsLink) throw new Error("missing action details link");
  await act(async () => detailsLink.click());

  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe(`/actions/${ROW.id}`);
  expect(service.get).toHaveBeenCalledWith(ROW.id);
  expect(container.textContent).toContain("Exact arguments (unredacted)");
  expect(container.textContent).toContain("exact-command");

  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/threads/example-thread");
  expect(container.textContent).toContain("Thread contents remain open");
});

it("falls back to the Actions page when detail was opened without an in-app origin", async () => {
  const service: ActionService = {
    list: vi.fn(async () => []),
    get: vi.fn(async () => ROW),
    decide: vi.fn(async (item) => item),
  };
  const container = await mount(<App initialPath={`/actions/${ROW.id}`} service={service} requests={[]} />);
  const back = [...container.querySelectorAll("button")].find((candidate) => candidate.textContent?.trim() === "Back");
  if (!back) throw new Error("missing action detail Back button");
  await act(async () => back.click());
  expect(container.querySelector('[data-testid="current-path"]')?.textContent).toBe("/actions");
  expect(container.textContent).toContain("All actions");
});

afterEach(unmountLast);
