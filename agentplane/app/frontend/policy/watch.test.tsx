// @vitest-environment happy-dom
import { MantineProvider } from "@mantine/core";
import { create, type JsonValue } from "@bufbuild/protobuf";
import { act, type JSX } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import { useIdentities, usePolicyResources, type BindingSelector } from "./watch";
import type { WatchHandlers } from "../watch_stream";
import { ServiceAccountSchema } from "../../../proto/identity_pb";

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
  return (
    <Selected
      selector={{ case: "subject", value: create(ServiceAccountSchema, { namespace: "agents", name: subject }) }}
      fence={fence}
    />
  );
}
function Selected({ selector, fence }: { selector: BindingSelector; fence: string }) {
  const result = usePolicyResources(selector, fence);
  return (
    <output>
      {result.error ??
        (result.resources
          ? `${result.resources.bindings.map((b) => b.identity?.uid).join(",")}/${result.resources.sets.length}`
          : "loading")}
    </output>
  );
}
function Identities() {
  const result = useIdentities();
  return (
    <output>{result.error ?? (result.identities ? result.identities.map((i) => i.identity?.uid).join(",") : "loading")}</output>
  );
}
async function mount(node: JSX.Element): Promise<void> {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<MantineProvider>{node}</MantineProvider>));
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

it.each<[BindingSelector, string]>([
  [{ case: "identity", value: "fake-agent" }, "/upstream/policy/v1/policy-bindings:watch?identity=fake-agent"],
  [{ case: "everyIdentity", value: true }, "/upstream/policy/v1/policy-bindings:watch?everyIdentity=true"],
])("watches the bindings an identity selector names", async (selector, url) => {
  await mount(<Selected selector={selector} fence="" />);
  expect([...watches.keys()].sort()).toEqual([url, "/upstream/policy/v1/policy-sets:watch"]);
});

it("follows identities from a completed snapshot and fences removals by UID", async () => {
  await mount(<Identities />);
  expect([...watches.keys()]).toEqual(["/upstream/policy/v1/identities:watch"]);
  await act(async () => {
    start();
    frame("identities", { snapshot: { identities: [{ identity: { namespace: "policies", name: "agent", uid: "old" } }] } });
  });
  expect(container.querySelector("output")?.textContent).toBe("loading");
  await act(async () => frame("identities", { snapshotComplete: {} }));
  expect(container.querySelector("output")?.textContent).toBe("old");
  await act(async () => frame("identities", { removed: { namespace: "policies", name: "agent", uid: "other" } }));
  expect(container.querySelector("output")?.textContent).toBe("old");
  await act(async () => frame("identities", { removed: { namespace: "policies", name: "agent", uid: "old" } }));
  expect(container.querySelector("output")?.textContent).toBe("");
});
