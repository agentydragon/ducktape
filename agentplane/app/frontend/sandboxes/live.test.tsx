// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { serveEventStreams, type ServedWatch } from "../testing/watch";
import { SandboxesLiveProvider, useRequiredSandboxesLive } from "./live";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function Inventory() {
  const { snapshot } = useRequiredSandboxesLive();
  return (
    <span>
      {snapshot === null ? "waiting" : snapshot.sandboxes.map((sandbox) => `${sandbox.name}:${sandbox.uid}`).join(",")}
    </span>
  );
}

it("publishes a complete snapshot then applies upserts and UID-fenced removals", async () => {
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
  await act(async () =>
    root.render(
      <SandboxesLiveProvider>
        <Inventory />
      </SandboxesLiveProvider>
    )
  );
  expect(container.textContent).toBe("waiting");
  await act(async () => {
    watch?.send({ snapshot: { sandboxes: [{ name: "box", uid: "old" }] } });
  });
  expect(container.textContent).toBe("waiting");
  await act(async () => {
    watch?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("box:old");
  await act(async () => {
    watch?.send({ upsert: { name: "box", uid: "new" } });
  });
  expect(container.textContent).toBe("box:new");
  await act(async () => {
    watch?.send({ removed: { name: "box", uid: "old" } });
  });
  expect(container.textContent).toBe("box:new");
  await act(async () => {
    watch?.send({ upsert: { name: "second", uid: "u2" } });
  });
  expect(container.textContent).toBe("box:new,second:u2");
  await act(async () => {
    watch?.send({ removed: { name: "box", uid: "new" } });
  });
  expect(container.textContent).toBe("second:u2");
  await act(async () => root.unmount());
});

it("retains the previous inventory until a reconnect snapshot is complete", async () => {
  vi.useFakeTimers();
  const watches: ServedWatch[] = [];
  serveEventStreams(
    (stream) => {
      watches.push(stream);
    },
    undefined,
    new Set(["/upstream/environments/v1/environments:watch"])
  );
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <SandboxesLiveProvider>
        <Inventory />
      </SandboxesLiveProvider>
    )
  );
  await act(async () => {
    watches[0]?.send({ snapshot: { sandboxes: [{ name: "old", uid: "u1" }] } });
    watches[0]?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("old:u1");
  await act(async () => {
    watches[0]?.drop();
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(watches).toHaveLength(2);
  await act(async () => {
    watches[1]?.send({ snapshot: { sandboxes: [{ name: "new", uid: "u2" }] } });
  });
  expect(container.textContent).toBe("old:u1");
  await act(async () => {
    watches[1]?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("new:u2");
  await act(async () => root.unmount());
});
