// @vitest-environment happy-dom
import { create } from "@bufbuild/protobuf";
import { MantineProvider } from "@mantine/core";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { usePolicyResources, type BindingSelector } from "./watch";
import { latestOn, stubWatchFetch } from "../testing/watch";
import { ServiceAccountSchema } from "../../../proto/identity_pb";

const selector: BindingSelector = {
  case: "subject",
  value: create(ServiceAccountSchema, { namespace: "agents", name: "alice" }),
};

function Policy() {
  const { resources, error } = usePolicyResources(selector, "identity-uid");
  return (
    <output>
      {error ?? (resources ? resources.bindings.map((binding) => binding.identity?.uid).join(",") : "loading")}
    </output>
  );
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("drops stale policy on stream loss and publishes only a completed replacement snapshot", async () => {
  vi.useFakeTimers();
  const watches = stubWatchFetch().served;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(
        <MantineProvider>
          <Policy />
        </MantineProvider>
      );
      await vi.advanceTimersByTimeAsync(0);
    });
    const bindingsPath = "/upstream/policy/v1/policy-bindings:watch";
    const setsPath = "/upstream/policy/v1/policy-sets:watch";
    const originalBindings = latestOn(watches, bindingsPath);
    const originalSets = latestOn(watches, setsPath);
    await act(async () => {
      originalBindings.send({
        snapshot: { bindings: [{ identity: { namespace: "policies", name: "grant", uid: "old" } }] },
      });
      originalBindings.send({ snapshotComplete: {} });
      originalSets.send({ snapshotComplete: {} });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.querySelector("output")?.textContent).toBe("old");

    await act(async () => {
      originalBindings.drop();
      originalSets.drop();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.querySelector("output")?.textContent).not.toBe("old");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    const replacementBindings = latestOn(watches, bindingsPath);
    const replacementSets = latestOn(watches, setsPath);
    expect(replacementBindings).not.toBe(originalBindings);
    expect(replacementSets).not.toBe(originalSets);
    await act(async () => {
      replacementBindings.send({
        snapshot: { bindings: [{ identity: { namespace: "policies", name: "grant", uid: "new" } }] },
      });
      replacementBindings.send({ snapshotComplete: {} });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.querySelector("output")?.textContent).toBe("loading");
    await act(async () => {
      replacementSets.send({ snapshotComplete: {} });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(container.querySelector("output")?.textContent).toBe("new");
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});
