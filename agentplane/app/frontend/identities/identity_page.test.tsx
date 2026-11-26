// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { act } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { cleanupMounted, renderWithMantine } from "../../../../util/frontend/testing/react";
import { IdentitySchema, PolicyBindingSchema, type Identity } from "../../../policy/v1/policy_service_pb";
import { IdentityPage } from "./identity_page";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn(), remove: vi.fn(), check: vi.fn() }));
vi.mock("../sandboxes/live", () => ({
  useRequiredSandboxesLive: () => ({ snapshot: [] }),
  sandboxInventoryFresh: () => true,
}));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../egress/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
vi.mock("./client", () => ({ deleteIdentity: mocks.remove, checkIdentityDelete: mocks.check }));
let container: HTMLDivElement;
afterEach(async () => {
  await cleanupMounted();
  vi.clearAllMocks();
});

function identity(name: string, labels: Record<string, string> = {}): Identity {
  return create(IdentitySchema, {
    identity: { namespace: "fake-policies", name, uid: `uid-${name}` },
    labels,
    spec: { description: `Fake ${name}` },
  });
}
function binding(name: string, subject: string, ownerUid: string | null) {
  return create(PolicyBindingSchema, {
    identity: { namespace: "fake-policies", name, uid: `uid-${name}` },
    ownerReferences: ownerUid ? [{ kind: "Identity", name: subject, uid: ownerUid }] : [],
    spec: { subject: { identity: { name: subject } }, policySets: ["base"] },
  });
}
const onDeleted = vi.fn();
async function render(
  subject: Identity,
  bindings = [binding("own-launch", subject.identity!.name, subject.identity!.uid)]
) {
  mocks.identities.mockReturnValue({ identities: [subject], error: null });
  mocks.resources.mockReturnValue({ resources: { bindings, sets: [] }, error: null });
  container = await renderWithMantine(
    <MemoryRouter>
      <IdentityPage name={subject.identity!.name} onBack={() => {}} onDeleted={onDeleted} />
    </MemoryRouter>
  );
}
function deleteButton(): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((item) => item.textContent === "Delete identity");
  if (!found) throw new Error("no Delete identity button");
  return found;
}
function confirmButton(): HTMLButtonElement {
  const found = [...document.querySelectorAll("button")].find((item) => item.textContent === "Delete");
  if (!found) throw new Error("no Delete confirm button");
  return found;
}
function referencing(): string[] {
  // The confirm modal renders its refusal in a portal outside the page container.
  return [...document.querySelectorAll("[aria-label='References to this identity'] li")].map(
    (item) => item.textContent ?? ""
  );
}

it("watches the bindings naming the identity, fenced by its UID", async () => {
  await render(identity("unreferenced-identity"));
  expect(mocks.resources).toHaveBeenCalledWith(
    { case: "identity", value: "unreferenced-identity" },
    "uid-unreferenced-identity"
  );
});

it("deletes an identity the Policy Controller would accept, after confirmation", async () => {
  mocks.check.mockResolvedValue({ case: "allowed" });
  mocks.remove.mockResolvedValue({ case: "deleted" });
  const subject = identity("unreferenced-identity");
  await render(subject);
  await act(async () => deleteButton().click());
  expect(mocks.check).toHaveBeenCalledWith(subject);
  expect(mocks.remove).not.toHaveBeenCalled();
  await act(async () => confirmButton().click());
  expect(mocks.remove).toHaveBeenCalledWith(subject);
  expect(onDeleted).toHaveBeenCalled();
});

it.each([
  [
    "a referenced identity, naming the references",
    { case: "referenced", references: [{ kind: "WorkloadCredential", name: "fake-workloads/standing-agent" }] },
    ["WorkloadCredential fake-workloads/standing-agent"],
  ],
  ["a git-origin identity", { case: "failed", error: "Identity is managed by Flux" }, []],
])("withholds the confirm for %s when the dry run refuses", async (_, refusal, named) => {
  mocks.check.mockResolvedValue(refusal);
  await render(identity("refused-identity"));
  expect(deleteButton().disabled).toBe(false);
  await act(async () => deleteButton().click());
  expect(confirmButton().disabled).toBe(true);
  expect(referencing()).toEqual(named);
  expect(mocks.remove).not.toHaveBeenCalled();
});

it("names what the Policy Controller cites when it refuses a delete the dry run allowed", async () => {
  mocks.check.mockResolvedValue({ case: "allowed" });
  mocks.remove.mockResolvedValue({
    case: "referenced",
    references: [
      { kind: "PolicyBinding", name: "created-meanwhile" },
      { kind: "WorkloadCredential", name: "fake-workloads/standing-agent" },
    ],
  });
  await render(identity("unreferenced-identity"));
  await act(async () => deleteButton().click());
  await act(async () => confirmButton().click());
  expect(onDeleted).not.toHaveBeenCalled();
  expect(referencing()).toEqual([
    "PolicyBinding created-meanwhile",
    "WorkloadCredential fake-workloads/standing-agent",
  ]);
});
