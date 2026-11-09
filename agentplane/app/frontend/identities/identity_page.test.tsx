// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import { IdentitySchema, PolicyBindingSchema, type Identity } from "../../../policy/v1/policy_service_pb";
import { IdentityPage } from "./identity_page";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn(), remove: vi.fn() }));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../bindings/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
vi.mock("./client", () => ({ deleteIdentity: mocks.remove }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
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
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <IdentityPage name={subject.identity!.name} onBack={() => {}} onDeleted={onDeleted} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
}
function deleteButton(): HTMLButtonElement {
  const found = [...container.querySelectorAll("button")].find((item) => item.textContent === "Delete identity");
  if (!found) throw new Error("no Delete identity button");
  return found;
}
function referencing(): string[] {
  return [...container.querySelectorAll("[aria-label='Bindings referencing this identity'] li")].map(
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

it("deletes an unreferenced identity after confirmation", async () => {
  mocks.remove.mockResolvedValue({ case: "deleted" });
  const subject = identity("unreferenced-identity");
  await render(subject);
  expect(deleteButton().disabled).toBe(false);
  await act(async () => deleteButton().click());
  const confirm = [...document.querySelectorAll("button")].find((item) => item.textContent === "Delete");
  await act(async () => confirm!.click());
  expect(mocks.remove).toHaveBeenCalledWith(subject);
  expect(onDeleted).toHaveBeenCalled();
});

it("refuses to delete a referenced identity and names what references it", async () => {
  const subject = identity("referenced-identity");
  await render(subject, [
    binding("own-launch", "referenced-identity", "uid-referenced-identity"),
    binding("standing-grant", "referenced-identity", null),
  ]);
  expect(deleteButton().disabled).toBe(true);
  expect(referencing()).toEqual(["PolicyBinding standing-grant"]);
});

it("keeps a git-origin identity read-only", async () => {
  await render(identity("git-origin-identity", { "kustomize.toolkit.fluxcd.io/name": "fake-kustomization" }));
  expect(deleteButton().disabled).toBe(true);
});

it("names the bindings the Policy Controller cites when it refuses a delete the view allowed", async () => {
  mocks.remove.mockResolvedValue({ case: "referenced", bindings: ["created-meanwhile"] });
  await render(identity("unreferenced-identity"));
  await act(async () => deleteButton().click());
  const confirm = [...document.querySelectorAll("button")].find((item) => item.textContent === "Delete");
  await act(async () => confirm!.click());
  expect(onDeleted).not.toHaveBeenCalled();
  expect(referencing()).toEqual(["PolicyBinding created-meanwhile"]);
});
