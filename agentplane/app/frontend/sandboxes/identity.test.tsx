// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { HttpError } from "../client";
import { EnvironmentIdentity } from "./identity";

const calls = vi.hoisted(() => ({
  get: vi.fn(async () => null as string | null),
  set: vi.fn(async (_target: unknown, _name: string | null) => {}),
}));
vi.mock("./client", () => ({ getEnvironmentIdentityRecord: calls.get, setEnvironmentIdentity: calls.set }));
vi.mock("../policy/watch", () => ({
  useIdentities: () => ({ identities: [{ identity: { name: "next-identity" } }], error: null }),
}));

const container = document.createElement("div");
document.body.append(container);
const root = createRoot(container);

function sandbox(stopped: boolean) {
  return create(EnvironmentDescriptorSchema, {
    target: { namespace: "team", name: "box", uid: "box-uid" },
    identityName: "prior-identity",
    observedState: { stopStage: stopped ? "Stopped" : "" },
  });
}

async function render(stopped: boolean): Promise<void> {
  await act(async () => {
    root.render(
      <MantineProvider env="test">
        <EnvironmentIdentity sandbox={sandbox(stopped)} />
      </MantineProvider>
    );
  });
}

async function selectNext(): Promise<void> {
  const field = identityField();
  if (!field) throw new Error("missing identity selector");
  await act(async () => field.click());
  const list = document.getElementById(field.getAttribute("aria-controls") ?? "");
  const option = [...(list?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])].find(
    (node) => node.textContent === "next-identity"
  );
  if (!option) throw new Error("missing next identity option");
  await act(async () => option.click());
}

function identityField(): HTMLInputElement | null {
  const label = [...container.querySelectorAll("label")].find((node) => node.textContent === "Identity for next start");
  return label?.control instanceof HTMLInputElement ? label.control : null;
}

afterEach(async () => {
  await act(async () => root.render(<></>));
  calls.get.mockClear();
  calls.set.mockReset();
});

it("shows the requested identity and checked credential without a running change control", async () => {
  calls.get.mockResolvedValueOnce("prior-identity");
  await render(false);
  expect(container.textContent).toContain("Current: prior-identity");
  expect(container.textContent).toContain("Credential at last check: prior-identity");
  expect(identityField()).toBeNull();
});

it("warns before a stopped change and preserves an admission refusal after the stop state changes", async () => {
  calls.get.mockResolvedValue(null);
  calls.set.mockRejectedValueOnce(
    new HttpError(new Response(null, { status: 400 }), {
      code: 9,
      message: "spec.identity changes only while nothing runs",
    })
  );
  await render(true);
  await selectNext();
  expect(container.textContent).toContain("earlier agents left in this workspace");
  const button = [...container.querySelectorAll("button")].find((node) => node.textContent === "Change identity");
  if (!button) throw new Error("missing change button");
  await act(async () => button.click());
  expect(calls.set).toHaveBeenCalledWith(expect.objectContaining({ uid: "box-uid" }), "next-identity");
  await render(false);
  expect(identityField()).toBeNull();
  expect(container.textContent).toContain("spec.identity changes only while nothing runs");
});
