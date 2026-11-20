// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { cleanupMounted, renderWithMantine } from "../../../../util/frontend/testing/react";
import { IdentitySchema, PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { IdentityList } from "./list";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn() }));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../egress/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
let container: HTMLDivElement;
afterEach(cleanupMounted);

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
  container = await renderWithMantine(
    <MemoryRouter initialEntries={[route]}>
      <IdentityList onOpen={() => {}} />
    </MemoryRouter>
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
