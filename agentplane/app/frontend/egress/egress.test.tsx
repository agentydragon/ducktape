// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { ServiceAccountSchema } from "../../../proto/identity_pb";
import { stubWatchFetch } from "../testing/watch";
import { EgressLog } from "./egress";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("reads generated snapshots, clears on disconnect, and cancels when the account changes", async () => {
  vi.useFakeTimers();
  const fetch = stubWatchFetch();
  const container = document.createElement("div");
  const root = createRoot(container);
  const render = (name: string) =>
    root.render(
      <MantineProvider>
        <MemoryRouter>
          <EgressLog key={name} sandbox="box" account={create(ServiceAccountSchema, { namespace: "test", name })} />
        </MemoryRouter>
      </MantineProvider>
    );
  await act(async () => render("first"));
  expect(fetch.served[0]?.url.searchParams.get("filter.subject.name")).toBe("first");
  await act(async () =>
    fetch.served[0]?.send({
      snapshot: {
        decisions: [
          {
            eventId: "first-id",
            at: "2026-09-02T10:00:01Z",
            method: "GET",
            host: "first.example",
            port: 443,
            outcome: "OUTCOME_ALLOW",
          },
        ],
      },
    })
  );
  expect(container.textContent).toContain("first.example");
  await act(async () =>
    fetch.served[0]?.send({
      delta: {
        removedEventIds: ["first-id"],
        added: [
          {
            eventId: "second-id",
            at: "2026-09-02T09:00:00Z",
            method: "GET",
            host: "late.example",
            port: 443,
            outcome: "OUTCOME_DENY",
          },
        ],
      },
    })
  );
  expect(container.textContent).not.toContain("first.example");
  expect(container.textContent).toContain("late.example");
  await act(async () => fetch.served[0]?.drop());
  expect(container.textContent).not.toContain("late.example");
  expect(container.textContent).toContain("unavailable");
  await act(async () => render("second"));
  expect(fetch.served[0]?.signal?.aborted).toBe(true);
  expect(fetch.served[1]?.url.searchParams.get("filter.subject.name")).toBe("second");
  await act(async () => fetch.served[1]?.send({ snapshot: { decisions: [] } }));
  expect(container.textContent).toContain("No decisions yet.");
  await act(async () => root.unmount());
});
