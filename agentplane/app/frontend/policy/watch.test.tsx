// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import type { JsonValue } from "@bufbuild/protobuf";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { usePolicyResources } from "./watch";
import type { WatchHandlers } from "../watch_stream";

const watches = vi.hoisted(() => new Map<string, WatchHandlers>());
vi.mock("../watch_stream", () => ({
  followWatch: (url: string, handlers: WatchHandlers) => {
    watches.set(url, handlers);
    return () => watches.delete(url);
  },
}));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  watches.clear();
});
function Host({ subject, fence }: { subject: string; fence: string }) {
  const result = usePolicyResources({ namespace: "agents", name: subject }, fence);
  return (
    <output>
      {result.error ??
        (result.resources
          ? `${result.resources.bindings.map((b) => b.identity?.uid).join(",")}/${result.resources.sets.length}`
          : "loading")}
    </output>
  );
}
function handler(fragment: string): WatchHandlers {
  const value = [...watches].find(([url]) => url.includes(fragment))?.[1];
  if (!value) throw new Error(`missing watch ${fragment}`);
  return value;
}
function start(): void {
  for (const item of watches.values()) item.onOpen();
}
function frame(fragment: string, result: JsonValue): void {
  handler(fragment).onFrame(result);
}

it("waits for both snapshots, fences removals, and clears stale data on reconnect or subject change", async () => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MantineProvider>
        <Host subject="alice" fence="old" />
      </MantineProvider>
    )
  );
  expect([...watches.keys()].sort()).toEqual([
    "/upstream/policy/v1/policy-bindings:watch?subject.namespace=agents&subject.name=alice",
    "/upstream/policy/v1/policy-sets:watch",
  ]);
  await act(async () => {
    start();
    frame("policy-bindings", {
      snapshot: { bindings: [{ identity: { namespace: "policies", name: "grant", uid: "old" } }] },
    });
    frame("policy-bindings", { snapshotComplete: {} });
  });
  expect(container.querySelector("output")?.textContent).toBe("loading");
  await act(async () => {
    frame("policy-sets", { snapshotComplete: {} });
  });
  expect(container.querySelector("output")?.textContent).toBe("old/0");
  await act(async () => {
    frame("policy-bindings", { upsert: { identity: { namespace: "policies", name: "grant", uid: "new" } } });
    frame("policy-bindings", { removed: { namespace: "policies", name: "grant", uid: "old" } });
  });
  expect(container.querySelector("output")?.textContent).toBe("new/0");
  await act(async () =>
    handler("policy-sets").onConnection({ phase: "reconnecting", since: Date.now(), attempt: 1, lastError: "lost" })
  );
  expect(container.querySelector("output")?.textContent).toBe("lost");
  await act(async () =>
    root.render(
      <MantineProvider>
        <Host subject="bob" fence="new" />
      </MantineProvider>
    )
  );
  expect(container.querySelector("output")?.textContent).toBe("loading");
});
