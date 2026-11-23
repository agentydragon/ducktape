// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { act } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { cleanupMounted, mountWithMantine } from "../../../../util/frontend/testing/react";

import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { HttpError } from "../transport/client";
import { EnvironmentIdentity } from "./identity";

const calls = vi.hoisted(() => ({
  get: vi.fn(async () => null as string | null),
  set: vi.fn(async (_target: unknown, _name: string | null) => {}),
}));
vi.mock("./client", () => ({ getEnvironmentIdentityRecord: calls.get, setEnvironmentIdentity: calls.set }));
vi.mock("../policy/watch", () => ({
  useIdentities: () => ({ identities: [{ identity: { name: "next-identity" } }], error: null }),
}));

function sandbox(stopped: boolean) {
  return create(EnvironmentDescriptorSchema, {
    target: { namespace: "team", name: "box", uid: "box-uid" },
    identityName: "prior-identity",
    observedState: { stopStage: stopped ? "Stopped" : "" },
  });
}

async function selectNext(container: HTMLDivElement): Promise<void> {
  const field = identityField(container);
  if (!field) throw new Error("missing identity selector");
  await act(async () => field.click());
  const list = document.getElementById(field.getAttribute("aria-controls") ?? "");
  const option = [...(list?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])].find(
    (node) => node.textContent === "next-identity"
  );
  if (!option) throw new Error("missing next identity option");
  await act(async () => option.click());
}

function identityField(container: HTMLDivElement): HTMLInputElement | null {
  const label = [...container.querySelectorAll("label")].find((node) => node.textContent === "Identity for next start");
  return label?.control instanceof HTMLInputElement ? label.control : null;
}

afterEach(async () => {
  await cleanupMounted();
  calls.get.mockClear();
  calls.set.mockReset();
});

it("shows the requested identity and checked credential without a running change control", async () => {
  calls.get.mockResolvedValueOnce("prior-identity");
  const { container } = await mountWithMantine(<EnvironmentIdentity sandbox={sandbox(false)} />);
  expect(container.textContent).toContain("Current: prior-identity");
  expect(container.textContent).toContain("Credential at last check: prior-identity");
  expect(identityField(container)).toBeNull();
});

it("warns before a stopped change and preserves an admission refusal after the stop state changes", async () => {
  calls.get.mockResolvedValue(null);
  calls.set.mockRejectedValueOnce(
    new HttpError(new Response(null, { status: 400 }), {
      code: 9,
      message: "spec.identity changes only while nothing runs",
    })
  );
  const { container, rerender } = await mountWithMantine(<EnvironmentIdentity sandbox={sandbox(true)} />);
  await selectNext(container);
  expect(container.textContent).toContain("earlier agents left in this workspace");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "Change identity");
  if (!button) throw new Error("missing change button");
  await act(async () => button.click());
  expect(calls.set).toHaveBeenCalledWith(expect.objectContaining({ uid: "box-uid" }), "next-identity");
  await rerender(<EnvironmentIdentity sandbox={sandbox(false)} />);
  expect(identityField(container)).toBeNull();
  expect(container.textContent).toContain("spec.identity changes only while nothing runs");
});
