// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { IdentitySchema } from "../../../policy/v1/policy_service_pb";
import { EnvironmentTargetSchema } from "../../../proto/identity_pb";
import { ConfirmDelete } from "./lifecycle";

const mocks = vi.hoisted(() => ({
  inventory: null as unknown,
  identities: null as unknown,
  policy: null as unknown,
  sessions: vi.fn(),
  deleteIdentity: vi.fn(),
  deleteEnvironment: vi.fn(),
}));
vi.mock("./live", () => ({
  useRequiredSandboxesLive: () => mocks.inventory,
  sandboxInventoryFresh: (live: { stream: { standing: string }; health: { healthy: boolean } }) =>
    live.stream.standing === "current" && live.health.healthy,
}));
vi.mock("../policy/watch", () => ({
  useIdentities: () => ({ identities: mocks.identities, error: null }),
  usePolicyResources: () => ({ resources: mocks.policy, error: null }),
}));
vi.mock("../identities/client", () => ({
  listIdentitySessions: mocks.sessions,
  deleteIdentity: mocks.deleteIdentity,
}));
vi.mock("./client", () => ({ deleteEnvironment: mocks.deleteEnvironment }));

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("waits for Sandbox removal and rechecks references before retrying identity deletion", async () => {
  const target = create(EnvironmentTargetSchema, { namespace: "tools", name: "sandbox", uid: "sandbox-uid" });
  const sandbox = create(EnvironmentDescriptorSchema, { target, identityName: "reader" });
  mocks.inventory = {
    snapshot: [sandbox],
    health: { healthy: true, failing: {} },
    stream: { standing: "current", connection: { phase: "live", since: 0 } },
  };
  mocks.identities = [create(IdentitySchema, { identity: { namespace: "tools", name: "reader", uid: "reader-uid" } })];
  mocks.policy = { bindings: [], sets: [] };
  mocks.sessions.mockResolvedValue([]);
  mocks.deleteEnvironment.mockResolvedValue(undefined);
  mocks.deleteIdentity.mockResolvedValue({ case: "deleted" });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onDeleted = vi.fn();
  const render = async () =>
    act(async () =>
      root.render(
        <MantineProvider defaultColorScheme="auto">
          <ConfirmDelete target={target} identityName="reader" onCancel={vi.fn()} onDeleted={onDeleted} />
        </MantineProvider>
      )
    );
  await render();
  const checkbox = document.querySelector<HTMLInputElement>('input[type="checkbox"]');
  expect(checkbox?.disabled).toBe(false);
  await act(async () => checkbox?.click());
  const button = (label: string): HTMLButtonElement => {
    const found = [...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button')].find(
      (item) => item.textContent === label
    );
    if (!found) throw new Error(`Missing ${label} button`);
    return found;
  };
  await act(async () => button("Delete").click());
  expect(mocks.deleteEnvironment).toHaveBeenCalledWith(target);
  expect(mocks.deleteIdentity).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain("Sandbox deletion has not completed in inventory");
  mocks.inventory = { ...(mocks.inventory as object), snapshot: [] };
  await render();
  await act(async () => button("Retry identity deletion").click());
  expect(mocks.deleteIdentity).toHaveBeenCalledTimes(1);
  expect(onDeleted).toHaveBeenCalledTimes(1);
  await act(async () => root.unmount());
  container.remove();
});
