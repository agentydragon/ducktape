// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import { useCredentialMetadata } from "./credential_watch";
import type { WatchHandlers } from "../watch_stream";

const watch = vi.hoisted(() => ({ handlers: null as WatchHandlers | null, stopped: false }));
vi.mock("../watch_stream", () => ({
  followWatch: (_url: string, handlers: WatchHandlers) => {
    watch.handlers = handlers;
    return () => {
      watch.stopped = true;
    };
  },
}));
(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
function Host() {
  const credentials = useCredentialMetadata();
  return (
    <output>
      {credentials
        ? [...credentials.values()].map((item) => `${item.name}:${item.description}`).join(",")
        : "unavailable"}
    </output>
  );
}

it("replaces snapshots and removes stale details on disconnect", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<Host />));
  const handlers = watch.handlers;
  if (!handlers) throw new Error("watch not started");
  await act(async () =>
    handlers.onFrame({ snapshot: { credentials: [{ name: "github", uid: "old", description: "first" }] } })
  );
  expect(container.textContent).toBe("unavailable");
  await act(async () => handlers.onFrame({ snapshotComplete: {} }));
  expect(container.textContent).toBe("github:first");
  await act(async () => handlers.onFrame({ upsert: { name: "github", uid: "new", description: "updated" } }));
  expect(container.textContent).toBe("github:updated");
  await act(async () => handlers.onFrame({ removed: { name: "github", uid: "old" } }));
  expect(container.textContent).toBe("github:updated");
  await act(async () => handlers.onFrame({ removed: { name: "github", uid: "new" } }));
  expect(container.textContent).toBe("");
  await act(async () =>
    handlers.onConnection({ phase: "reconnecting", since: Date.now(), attempt: 1, lastError: "lost" })
  );
  expect(container.textContent).toBe("unavailable");
  await act(async () => root.unmount());
  expect(watch.stopped).toBe(true);
  container.remove();
});
