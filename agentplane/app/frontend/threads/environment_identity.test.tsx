// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";

import {
  IdentitySchema,
  PolicyBindingSchema,
  type Identity,
  type PolicyBinding,
} from "../../../policy/v1/policy_service_pb";
import { EnvironmentIdentity } from "./environment_identity";

const mocks = vi.hoisted(() => ({ identities: vi.fn(), resources: vi.fn() }));
vi.mock("../policy/watch", () => ({ useIdentities: mocks.identities, usePolicyResources: mocks.resources }));
vi.mock("../bindings/credential_watch", () => ({ useCredentialMetadata: () => new Map() }));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

function identity(name: string): Identity {
  return create(IdentitySchema, {
    identity: { namespace: "fake-policies", name, uid: `uid-${name}` },
    spec: { description: `Fake ${name}` },
  });
}

function binding(name: string, subject: string): PolicyBinding {
  return create(PolicyBindingSchema, {
    identity: { namespace: "fake-policies", name, uid: `uid-${name}` },
    spec: { subject: { identity: { name: subject } }, policySets: ["fake-base"] },
  });
}

async function render(identityName: string | null | undefined): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <EnvironmentIdentity identityName={identityName} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
}

it("distinguishes an unavailable environment from one without an identity", async () => {
  mocks.identities.mockReturnValue({ identities: null, error: null });
  mocks.resources.mockReturnValue({ resources: null, error: null });
  await render(undefined);
  expect(container.textContent).toContain("Loading environment identity…");
  expect(mocks.identities).not.toHaveBeenCalled();
  expect(mocks.resources).not.toHaveBeenCalled();

  await act(async () =>
    root.render(
      <MantineProvider env="test">
        <MemoryRouter>
          <EnvironmentIdentity identityName={null} />
        </MemoryRouter>
      </MantineProvider>
    )
  );
  expect(container.textContent).toContain("No identity is assigned to this environment.");
  expect(mocks.identities).not.toHaveBeenCalled();
});

it("shows an identity-watch failure instead of treating it as no identity", async () => {
  mocks.identities.mockReturnValue({ identities: null, error: "fake watch failure" });
  mocks.resources.mockReturnValue({ resources: null, error: null });
  await render("fake-agent");
  expect(container.textContent).toContain("Could not load the environment identity");
  expect(container.textContent).toContain("fake watch failure");
});

it("keeps an incomplete identity snapshot in a loading state", async () => {
  mocks.identities.mockReturnValue({ identities: null, error: null });
  mocks.resources.mockReturnValue({ resources: null, error: null });
  await render("fake-agent");
  expect(container.textContent).toContain("Loading identity fake-agent…");
});

it("reports an environment reference whose identity is missing", async () => {
  mocks.identities.mockReturnValue({ identities: [], error: null });
  mocks.resources.mockReturnValue({ resources: null, error: null });
  await render("deleted-agent");
  expect(container.textContent).toContain("Environment identity is missing");
  expect(container.textContent).toContain("Identity deleted-agent is no longer available.");
  expect(mocks.resources).toHaveBeenCalledWith(undefined, undefined);
});

it("shows the identity's watched bindings read-only", async () => {
  const subject = identity("fake-agent");
  const grants = [binding("fake-agent-base", "fake-agent")];
  mocks.identities.mockReturnValue({ identities: [subject], error: null });
  mocks.resources.mockReturnValue({ resources: { bindings: grants, sets: [] }, error: null });
  await render("fake-agent");
  expect(mocks.resources).toHaveBeenCalledWith({ case: "identity", value: "fake-agent" }, "uid-fake-agent");
  expect(container.textContent).toContain("Fake fake-agent");
  expect(container.textContent).toContain("fake-agent-base");
  expect([...container.querySelectorAll("button")].some((button) => button.textContent?.includes("Revoke"))).toBe(
    false
  );
});

it("keeps an incomplete binding snapshot in a loading state", async () => {
  mocks.identities.mockReturnValue({ identities: [identity("fake-agent")], error: null });
  mocks.resources.mockReturnValue({ resources: null, error: null });
  await render("fake-agent");
  expect(container.textContent).toContain("Loading bindings for fake-agent…");
});

it("keeps a policy-watch failure visible and separates it from loading", async () => {
  const subject = identity("fake-agent");
  mocks.identities.mockReturnValue({ identities: [subject], error: null });
  mocks.resources.mockReturnValue({ resources: null, error: "fake binding failure" });
  await render("fake-agent");
  expect(container.textContent).toContain("Could not load identity bindings");
  expect(container.textContent).toContain("fake binding failure");
});
