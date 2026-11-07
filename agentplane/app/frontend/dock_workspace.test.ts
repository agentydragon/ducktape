import type { SerializedDockview } from "dockview-react";
import { afterEach, expect, it, vi } from "vitest";

import {
  closePane,
  defaultWorkspace,
  openPane,
  paneId,
  parseStoredWorkspace,
  serializeWorkspace,
  type DockWorkspace,
} from "./dock_workspace";

afterEach(() => {
  vi.restoreAllMocks();
});

function layoutOf(...ids: string[]): SerializedDockview {
  return {
    grid: { root: { type: "leaf", data: { views: ids, id: "1" } }, height: 600, width: 900, orientation: "HORIZONTAL" },
    panels: Object.fromEntries(ids.map((id) => [id, { id, contentComponent: "pane", title: id }])),
  } as unknown as SerializedDockview;
}

function threadWorkspace(): DockWorkspace {
  return openPane(openPane(defaultWorkspace(), { kind: "thread", threadId: "t-1" }), {
    kind: "action",
    requestId: "r-1",
  });
}

it("restores the saved panes, the active pane and a layout holding exactly those panes", () => {
  const workspace = { ...threadWorkspace(), layout: layoutOf("threads", "actions", "thread:t-1", "action:r-1") };
  expect(parseStoredWorkspace(serializeWorkspace(workspace))).toEqual(workspace);
});

it("keeps the panes but drops a layout that no longer names them", () => {
  const workspace = { ...threadWorkspace(), layout: layoutOf("threads", "actions", "thread:t-1", "thread:gone") };
  const restored = parseStoredWorkspace(serializeWorkspace(workspace));
  expect(restored?.panes).toEqual(workspace.panes);
  expect(restored?.layout).toBeNull();
});

it.each([
  ["not JSON", "{"],
  ["an unknown version", JSON.stringify({ version: 99, panes: [], activePaneId: null, layout: null })],
  [
    "an unknown pane kind",
    JSON.stringify({ version: 1, panes: [{ kind: "mosaic" }], activePaneId: null, layout: null }),
  ],
  [
    "an active pane that is not open",
    JSON.stringify({ version: 1, panes: [{ kind: "threads" }], activePaneId: "actions", layout: null }),
  ],
  [
    "a pane open twice",
    JSON.stringify({ version: 1, panes: [{ kind: "threads" }, { kind: "threads" }], activePaneId: null, layout: null }),
  ],
])("falls back to the default workspace for %s, with a warning", (_name, raw) => {
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  expect(parseStoredWorkspace(raw)).toBeNull();
  expect(warn).toHaveBeenCalledOnce();
});

it("opens a pane once and makes it the active one", () => {
  const workspace = threadWorkspace();
  const again = openPane(workspace, { kind: "thread", threadId: "t-1" });
  expect(again.panes).toEqual(workspace.panes);
  expect(again.activePaneId).toBe("thread:t-1");
});

it("hands the active pane to the one before it when it closes", () => {
  const closed = closePane(threadWorkspace(), "action:r-1");
  expect(closed.panes.map(paneId)).toEqual(["threads", "actions", "thread:t-1"]);
  expect(closed.activePaneId).toBe("thread:t-1");
});
