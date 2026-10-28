// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import type { CallerGrantView } from "../schema";
import { CallerGrants } from "./caller_grants";
import type { CallerGrantReader } from "./models";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});
const ACCOUNT = { namespace: "external", name: "caller" };
const EMPTY: CallerGrantView = {
  bindings: [],
  action_policy: { synced: true, auto_approve_if: [] },
};
async function render(read: CallerGrantReader): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <CallerGrants account={ACCOUNT} read={read} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
}
function button(name: string): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((node) => node.textContent === name);
  if (!found) throw new Error(`Missing ${name}`);
  return found;
}
it("reads by account and renders honest empty policy without mutation controls", async () => {
  const read = vi.fn<CallerGrantReader>(async () => EMPTY);
  await render(read);
  expect(read.mock.calls[0][0]).toEqual(ACCOUNT);
  expect(container.textContent).toContain("Grants for external/caller");
  expect(container.textContent).toContain("No binding names this ServiceAccount");
  expect(container.textContent).toContain("every Action from this ServiceAccount waits for the operator");
  expect(container.textContent).not.toContain("Revoke");
  expect(container.textContent).not.toContain("Grant policy sets");
});
it("preserves expired and missing policy set references even when Action policy is unavailable", async () => {
  await render(async () => ({
    bindings: [
      {
        name: "external-grants",
        provenance: "git",
        generation: 1,
        subjects: [ACCOUNT],
        conditions: [],
        policy_sets: [],
        missing_policy_sets: ["removed-policy"],
        expires_at: "2000-01-01T00:00:00Z",
      },
    ],
    action_policy: { kind: "unavailable", code: "operator_federation_not_configured", upstream: null },
  }));
  expect(container.textContent).toContain("external-grants");
  expect(container.textContent).toContain("removed-policy?");
  expect(container.querySelector("time")?.getAttribute("datetime")).toBe("2000-01-01T00:00:00.000Z");
  expect(container.textContent).toContain("operator_federation_not_configured");
  await act(async () => button("Rules").click());
  expect(container.textContent).toContain("removed-policy: no such PolicySet");
});
it("shows unsynced policy separately from a request failure and supports refresh", async () => {
  const read = vi
    .fn<CallerGrantReader>()
    .mockResolvedValueOnce({ ...EMPTY, action_policy: { synced: false, auto_approve_if: [] } })
    .mockRejectedValueOnce(new Error("read failed"))
    .mockResolvedValueOnce(EMPTY);
  await render(read);
  expect(container.textContent).toContain("watch has not synced");
  await act(async () => button("Refresh grants").click());
  expect(container.textContent).toContain("Could not load grants: read failed");
  expect(container.textContent).not.toContain("No binding names");
  await act(async () => button("Refresh grants").click());
  expect(container.textContent).toContain("No binding names");
  expect(container.textContent).not.toContain("read failed");
});
it("aborts stale refreshes and ignores their late results", async () => {
  let finish: (value: CallerGrantView) => void = () => {
    throw new Error("read not started");
  };
  const read = vi
    .fn<CallerGrantReader>()
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        })
    )
    .mockResolvedValueOnce(EMPTY);
  await render(read);
  expect(container.textContent).toContain("Loading grants");
  await act(async () => button("Refresh grants").click());
  expect(read.mock.calls[0][1].aborted).toBe(true);
  await act(async () => finish({ ...EMPTY, action_policy: { synced: false, auto_approve_if: [] } }));
  expect(container.textContent).not.toContain("watch has not synced");
});
