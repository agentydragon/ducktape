// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { ServiceAccountSchema } from "../../../proto/identity_pb";
import { PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { CallerGrants } from "./caller_grants";

const watch = vi.hoisted(() => vi.fn());
vi.mock("../actions/policy", () => ({ ActionPolicyWatch: () => null }));
vi.mock("../policy/watch", () => ({ usePolicyResources: watch }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
const account = create(ServiceAccountSchema, { namespace: "external", name: "caller" });
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});
async function render(): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <CallerGrants account={account} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
}
it("watches the selected subject and displays empty grants without mutation controls", async () => {
  watch.mockReturnValue({ resources: { bindings: [], sets: [] }, error: null });
  await render();
  expect(watch).toHaveBeenCalledWith({ case: "subject", value: account }, undefined);
  expect(container.textContent).toContain("No binding names this ServiceAccount");
  expect(container.textContent).not.toContain("Revoke");
});
it("shows missing referenced sets", async () => {
  const binding = create(PolicyBindingSchema, {
    identity: { namespace: "policies", name: "grant", uid: "1" },
    spec: { subject: { namespace: "external", name: "caller" }, policySets: ["removed"] },
  });
  watch.mockReturnValue({ resources: { bindings: [binding], sets: [] }, error: null });
  await render();
  expect(container.textContent).toContain("grant");
  expect(container.textContent).toContain("removed?");
});
it("does not show stale empty grants after the watch fails", async () => {
  watch.mockReturnValue({ resources: null, error: "watch failed" });
  await render();
  expect(container.textContent).toContain("Could not load grants: watch failed");
  expect(container.textContent).not.toContain("No binding names");
});
