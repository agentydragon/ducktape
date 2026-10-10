import { expect, it } from "vitest";

import {
  createDefaultLayout,
  dockPane,
  paneIds,
  parseStoredWorkspace,
  parseWorkspaceJson,
  resizeSplit,
  serializeWorkspace,
  type MosaicWorkspace,
} from "./mosaic_layout";

const initial: MosaicWorkspace = {
  panes: [
    { kind: "thread", id: "thread:t1", threadId: "t1" },
    { kind: "thread", id: "thread:t2", threadId: "t2" },
    { kind: "action", id: "action:a1", requestId: "a1" },
  ],
  layout: createDefaultLayout(["thread:t1", "thread:t2", "action:a1"]),
  activePaneId: "thread:t1",
};

it("starts with the first thread beside a stack of the second thread and action", () => {
  expect(initial.layout).toEqual({
    type: "split",
    id: "mosaic-split-1",
    direction: "horizontal",
    ratio: 0.5,
    first: { type: "pane", paneId: "thread:t1" },
    second: {
      type: "split",
      id: "mosaic-split-2",
      direction: "vertical",
      ratio: 0.5,
      first: { type: "pane", paneId: "thread:t2" },
      second: { type: "pane", paneId: "action:a1" },
    },
  });
});

it("docks an existing pane by removing its old leaf and collapsing empty splits", () => {
  const layout = dockPane(initial.layout, "action:a1", "thread:t1", "top");

  expect(layout).toEqual({
    type: "split",
    id: "mosaic-split-1",
    direction: "horizontal",
    ratio: 0.5,
    first: {
      type: "split",
      id: "mosaic-split-2",
      direction: "vertical",
      ratio: 0.5,
      first: { type: "pane", paneId: "action:a1" },
      second: { type: "pane", paneId: "thread:t1" },
    },
    second: { type: "pane", paneId: "thread:t2" },
  });
  expect(paneIds(layout)).toEqual(["action:a1", "thread:t1", "thread:t2"]);
});

it("adds a new pane beside an existing leaf and clamps split resizing", () => {
  const added = dockPane(initial.layout, "thread:t3", "thread:t1", "right");
  expect(paneIds(added)).toContain("thread:t3");
  expect(resizeSplit(added, "mosaic-split-3", 0.99)).toMatchObject({
    first: { ratio: 0.85 },
  });
});

it("round trips saved workspaces and rejects inconsistent pane trees", () => {
  expect(parseWorkspaceJson(serializeWorkspace(initial))).toEqual(initial);
  const docked = dockPane(initial.layout, "action:a1", "thread:t1", "top");
  const resized = resizeSplit(docked, "mosaic-split-1", 0.6);
  expect(parseWorkspaceJson(serializeWorkspace({ ...initial, layout: resized, activePaneId: "action:a1" }))).toEqual({
    ...initial,
    layout: resized,
    activePaneId: "action:a1",
  });
  expect(parseWorkspaceJson("not json")).toBeNull();
  expect(
    parseStoredWorkspace({ version: 1, panes: initial.panes, layout: null, activePaneId: "thread:missing" })
  ).toBeNull();
  expect(
    parseStoredWorkspace({
      version: 1,
      panes: initial.panes,
      layout: { type: "pane", paneId: "thread:t1" },
      activePaneId: "thread:t1",
    })
  ).toBeNull();
});

it("preserves an intentionally empty workspace", () => {
  const empty: MosaicWorkspace = { panes: [], layout: null, activePaneId: null };
  expect(parseWorkspaceJson(serializeWorkspace(empty))).toEqual(empty);
});

it("round trips the threads, pending actions, and action history panes", () => {
  const workspace: MosaicWorkspace = {
    panes: [
      { kind: "threads", id: "threads" },
      { kind: "actions", id: "actions" },
      { kind: "history", id: "history" },
    ],
    layout: createDefaultLayout(["threads", "actions", "history"]),
    activePaneId: "history",
  };

  expect(parseWorkspaceJson(serializeWorkspace(workspace))).toEqual(workspace);
});
