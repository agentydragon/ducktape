// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { act } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { cleanupMounted, renderWithMantine } from "../../../../util/frontend/testing/react";
import { IdentitySchema, PolicyBindingSchema } from "../../../policy/v1/policy_service_pb";
import { IdentityList } from "./list";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn(), remove: vi.fn(), check: vi.fn() }));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../egress/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
vi.mock("./client", () => ({ deleteIdentity: mocks.remove, checkIdentityDelete: mocks.check }));
let container: HTMLDivElement;
afterEach(async () => {
  await cleanupMounted();
  vi.clearAllMocks();
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

function trash(name: string): HTMLButtonElement {
  const found = container.querySelector<HTMLButtonElement>(`button[aria-label='Delete ${name}']`);
  if (!found) throw new Error(`no delete control for ${name}`);
  return found;
}

function confirmButton(): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((item) => item.textContent === "Delete");
  if (!found) throw new Error("no Delete confirm button");
  return found;
}

it("deletes from its row after the dry run and confirmation", async () => {
  mocks.check.mockResolvedValue({ case: "allowed" });
  mocks.remove.mockResolvedValue({ case: "deleted" });
  await render("/identities");
  await act(async () => trash("unreferenced-identity").click());
  expect(mocks.check).toHaveBeenCalledWith(identities[1]);
  expect(mocks.remove).not.toHaveBeenCalled();
  await act(async () => confirmButton().click());
  expect(mocks.remove).toHaveBeenCalledWith(identities[1]);
});

it("keeps every row's trash enabled and withholds the confirm when the dry run refuses", async () => {
  mocks.check.mockResolvedValue({
    case: "referenced",
    references: [{ kind: "PolicyBinding", name: "standing-grant" }],
  });
  await render("/identities");
  expect(trash("referenced-identity").disabled).toBe(false);
  await act(async () => trash("referenced-identity").click());
  expect(confirmButton().disabled).toBe(true);
  expect(mocks.remove).not.toHaveBeenCalled();
});
