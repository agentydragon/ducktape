// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { serveEventStreams, type ServedWatch } from "../testing/watch";
import { SandboxesLiveProvider, useRequiredSandboxesLive } from "./live";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => vi.unstubAllGlobals());

it("publishes only complete inventories, retaining the prior snapshot while chunks arrive", async () => {
  let watch: ServedWatch | undefined;
  serveEventStreams(
    (stream) => {
      watch = stream;
    },
    undefined,
    new Set(["/upstream/environments/v1/environments:watch"])
  );
  const container = document.createElement("div");
  const root = createRoot(container);
  function Inventory() {
    const { snapshot } = useRequiredSandboxesLive();
    return <span>{snapshot === null ? "waiting" : snapshot.sandboxes.map((sandbox) => sandbox.name).join(",")}</span>;
  }
  await act(async () =>
    root.render(
      <SandboxesLiveProvider>
        <Inventory />
      </SandboxesLiveProvider>
    )
  );
  expect(container.textContent).toBe("waiting");
  await act(async () => {
    watch?.send({ snapshot: { sandboxes: [{ name: "first" }] } });
  });
  expect(container.textContent).toBe("waiting");
  await act(async () => {
    watch?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("first");
  await act(async () => {
    watch?.send({ snapshot: { sandboxes: [{ name: "second" }] } });
  });
  expect(container.textContent).toBe("first");
  await act(async () => {
    watch?.send({ snapshot: { sandboxes: [{ name: "third" }] } });
    watch?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("second,third");
  await act(async () => {
    watch?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("");
  await act(async () => root.unmount());
});
