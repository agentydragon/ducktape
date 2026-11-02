// @vitest-environment happy-dom
import { create, toJson } from "@bufbuild/protobuf";
import {
  EnvironmentDescriptorSchema,
  TargetHandleSchema,
  type TargetHandle,
} from "../../../runner_gateway/environments/environment_service_pb";
import { testSandbox } from "../testing/sandbox";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { serveEventStreams, stubWatchFetch, type ServedWatch } from "../testing/watch";
import { SandboxesLiveProvider, useEnvironmentWatch, useRequiredSandboxesLive } from "./live";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function Inventory() {
  const { snapshot } = useRequiredSandboxesLive();
  return (
    <span>
      {snapshot === null
        ? "waiting"
        : snapshot.map((sandbox) => `${sandbox.target!.name}:${sandbox.target!.uid}`).join(",")}
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
    watch?.send({ snapshot: { environments: [toJson(EnvironmentDescriptorSchema, testSandbox("box", "old"))] } });
  });
  expect(container.textContent).toBe("waiting");
  await act(async () => {
    watch?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("box:old");
  await act(async () => {
    watch?.send({ upsert: toJson(EnvironmentDescriptorSchema, testSandbox("box", "new")) });
  });
  expect(container.textContent).toBe("box:new");
  await act(async () => {
    watch?.send({ removed: { namespace: "agentplane-test", name: "box", uid: "old" } });
  });
  expect(container.textContent).toBe("box:new");
  await act(async () => {
    watch?.send({ upsert: toJson(EnvironmentDescriptorSchema, testSandbox("second", "u2")) });
  });
  expect(container.textContent).toBe("box:new,second:u2");
  await act(async () => {
    watch?.send({ removed: { namespace: "agentplane-test", name: "box", uid: "new" } });
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
    watches[0]?.send({ snapshot: { environments: [toJson(EnvironmentDescriptorSchema, testSandbox("old", "u1"))] } });
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
    watches[1]?.send({ snapshot: { environments: [toJson(EnvironmentDescriptorSchema, testSandbox("new", "u2"))] } });
  });
  expect(container.textContent).toBe("old:u1");
  await act(async () => {
    watches[1]?.send({ snapshotComplete: {} });
  });
  expect(container.textContent).toBe("new:u2");
  await act(async () => root.unmount());
});

function Detail({ target }: { target: TargetHandle | undefined }) {
  const { environment } = useEnvironmentWatch(target);
  return <span>{environment?.target?.uid ?? "waiting"}</span>;
}

it("does not connect or repeatedly report a disabled environment watch", async () => {
  const { served } = stubWatchFetch();
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<Detail target={undefined} />));
  expect(container.textContent).toBe("waiting");
  expect(served).toHaveLength(0);
  await act(async () => root.unmount());
});

it("watches exact target identity and clears the old descriptor before observing a replacement", async () => {
  const { served } = stubWatchFetch();
  const target = create(TargetHandleSchema, { namespace: "test-namespace", name: "test-box", uid: "old" });
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<Detail target={target} />));
  expect(served[0]?.url.pathname).toBe("/upstream/environments/v1/environments/test-namespace/test-box:watch");
  expect(served[0]?.url.searchParams.get("target.uid")).toBe("old");
  await act(async () => {
    served[0]?.send({
      environment: toJson(EnvironmentDescriptorSchema, testSandbox("test-box", "old", "test-namespace")),
    });
  });
  expect(container.textContent).toBe("old");
  const replacement = create(TargetHandleSchema, { ...target, uid: "new" });
  await act(async () => root.render(<Detail target={replacement} />));
  expect(served[0]?.signal?.aborted).toBe(true);
  expect(container.textContent).toBe("waiting");
  expect(served[1]?.url.searchParams.get("target.uid")).toBe("new");
  await act(async () => {
    served[1]?.send({
      environment: toJson(EnvironmentDescriptorSchema, testSandbox("test-box", "new", "test-namespace")),
    });
  });
  expect(container.textContent).toBe("new");
  await act(async () => root.unmount());
});

it("rejects a generated detail frame for a different target", async () => {
  const { served } = stubWatchFetch();
  const target = create(TargetHandleSchema, { namespace: "test-namespace", name: "test-box", uid: "expected" });
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<Detail target={target} />));
  await act(async () => {
    served[0]?.send({
      environment: toJson(EnvironmentDescriptorSchema, testSandbox("test-box", "replacement", "test-namespace")),
    });
  });
  expect(container.textContent).toBe("waiting");
  await act(async () => root.unmount());
});
