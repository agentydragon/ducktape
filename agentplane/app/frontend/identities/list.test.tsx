// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { IdentitySchema, PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { IdentityList } from "./list";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn() }));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../bindings/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

const identities = ["referenced-identity", "unreferenced-identity"].map((name) =>
  create(IdentitySchema, { identity: { namespace: "fake-policies", name, uid: `uid-${name}` } })
);
const standingGrant = create(PolicyBindingSchema, {
  identity: { namespace: "fake-policies", name: "standing-grant", uid: "uid-standing-grant" },
  spec: { subject: { identity: { name: "referenced-identity" } }, policySets: ["base"] },
});

async function render(route: string): Promise<void> {
  mocks.identities.mockReturnValue({ identities, error: null });
  mocks.resources.mockReturnValue({ resources: { bindings: [standingGrant], sets: [] }, error: null });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter initialEntries={[route]}>
          <IdentityList onOpen={() => {}} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
}
function listed(): string[] {
  return [...container.querySelectorAll("tbody tr td:first-child button")].map((item) => item.textContent ?? "");
}

it("watches every identity's bindings in one stream", async () => {
  await render("/identities");
  expect(mocks.resources).toHaveBeenCalledWith({ case: "everyIdentity", value: true }, undefined);
});

it.each([
  ["/identities", ["referenced-identity", "unreferenced-identity"]],
  ["/identities?unreferenced=1", ["unreferenced-identity"]],
])("lists %s as %j", async (route, names) => {
  await render(route);
  expect(listed()).toEqual(names);
});
