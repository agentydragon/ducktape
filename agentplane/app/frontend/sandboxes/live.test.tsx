// @vitest-environment happy-dom
import { create, toJson } from "@bufbuild/protobuf";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { EnvironmentTargetSchema, type EnvironmentTarget } from "../../../proto/identity_pb";
import { EnvironmentDescriptorSchema } from "../../../environments/v1/environment_service_pb";
import { testSandbox } from "../testing/sandbox";

import { serveEventStreams, stubWatchFetch, type ServedWatch } from "../testing/watch";
import { SandboxesLiveProvider, useEnvironmentWatch, useRequiredSandboxesLive } from "./live";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const roots: Root[] = [];

afterEach(async () => {
  const mountedRoots = roots.splice(0);
  try {
    for (const root of mountedRoots) await act(async () => root.unmount());
  } finally {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  }
});

async function mount(element: ReactNode): Promise<{ container: HTMLDivElement; root: Root }> {
  const container = document.createElement("div");
  const root = createRoot(container);
  roots.push(root);
  await act(async () => root.render(element));
  return { container, root };
}

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
  const { container } = await mount(
    <SandboxesLiveProvider>
      <Inventory />
    </SandboxesLiveProvider>
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
  const { container } = await mount(
    <SandboxesLiveProvider>
      <Inventory />
    </SandboxesLiveProvider>
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
});

function Detail({ target }: { target: EnvironmentTarget | undefined }) {
  const { environment } = useEnvironmentWatch(target);
  return <span>{environment?.target?.uid ?? "waiting"}</span>;
}

it("does not connect or repeatedly report a disabled environment watch", async () => {
  const { served } = stubWatchFetch();
  const { container } = await mount(<Detail target={undefined} />);
  expect(container.textContent).toBe("waiting");
  expect(served).toHaveLength(0);
});

it("watches exact target identity and clears the old descriptor before observing a replacement", async () => {
  const { served } = stubWatchFetch();
  const target = create(EnvironmentTargetSchema, { namespace: "test-namespace", name: "test-box", uid: "old" });
  const { container, root } = await mount(<Detail target={target} />);
  expect(served[0]?.url.pathname).toBe("/upstream/environments/v1/environments/test-namespace/test-box:watch");
  expect(served[0]?.url.searchParams.get("target.uid")).toBe("old");
  await act(async () => {
    served[0]?.send({
      environment: toJson(EnvironmentDescriptorSchema, testSandbox("test-box", "old", "test-namespace")),
    });
  });
  expect(container.textContent).toBe("old");
  const replacement = create(EnvironmentTargetSchema, { ...target, uid: "new" });
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
});

it("rejects a generated detail frame for a different target", async () => {
  const { served } = stubWatchFetch();
  const target = create(EnvironmentTargetSchema, { namespace: "test-namespace", name: "test-box", uid: "expected" });
  const { container } = await mount(<Detail target={target} />);
  await act(async () => {
    served[0]?.send({
      environment: toJson(EnvironmentDescriptorSchema, testSandbox("test-box", "replacement", "test-namespace")),
    });
  });
  expect(container.textContent).toBe("waiting");
});
