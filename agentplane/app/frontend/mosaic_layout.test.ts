import { expect, it } from "vitest";

import {
  createDefaultWorkspace,
  insertPane,
  paneIds,
  parseSplitLayout,
  parseStoredWorkspace,
  parseWorkspaceJson,
  removePane,
  serializeWorkspace,
  type MosaicWorkspace,
} from "./mosaic_layout";

it("starts with both browser panes and one content thread", () => {
  const workspace = createDefaultWorkspace("newest-thread");
  expect(paneIds(workspace.layout)).toEqual(["threads", "actions", "thread:newest-thread"]);
  expect(workspace.activePaneId).toBe("thread:newest-thread");
  expect(workspace.layout).toMatchObject({
    type: "split",
    direction: "row",
    splitPercentages: [35, 65],
    children: [{ type: "split", direction: "column", splitPercentages: [65, 35] }, "thread:newest-thread"],
  });
  expect(paneIds(createDefaultWorkspace(null).layout)).toEqual(["threads", "actions"]);
});

it("inserts beside the active tile and collapses splits when a tile closes", () => {
  const layout = insertPane(createDefaultWorkspace("one").layout, "thread:one", "thread:two");
  expect(paneIds(layout)).toEqual(["threads", "actions", "thread:one", "thread:two"]);
  expect(paneIds(removePane(layout, "thread:two"))).toEqual(["threads", "actions", "thread:one"]);
  expect(paneIds(removePane(removePane(layout, "thread:one"), "thread:two"))).toEqual(["threads", "actions"]);
});

it("migrates the hand-rolled layout without losing its pane positions", () => {
  const old = {
    version: 1,
    panes: [
      { kind: "threads", id: "threads" },
      { kind: "actions", id: "actions" },
      { kind: "thread", id: "thread:one", threadId: "one" },
    ],
    layout: {
      type: "split",
      id: "mosaic-split-1",
      direction: "horizontal",
      ratio: 0.4,
      first: { type: "pane", paneId: "threads" },
      second: {
        type: "split",
        id: "mosaic-split-2",
        direction: "vertical",
        ratio: 0.3,
        first: { type: "pane", paneId: "actions" },
        second: { type: "pane", paneId: "thread:one" },
      },
    },
    activePaneId: "thread:one",
  };
  const migrated = parseStoredWorkspace(old);
  expect(migrated?.layout).toEqual({
    type: "split",
    direction: "row",
    splitPercentages: [40, 60],
    children: [
      "threads",
      { type: "split", direction: "column", splitPercentages: [30, 70], children: ["actions", "thread:one"] },
    ],
  });
  expect(parseWorkspaceJson(serializeWorkspace(migrated as MosaicWorkspace))).toEqual(migrated);
});

it("rejects tabs, duplicate tiles, and missing active tiles in saved data", () => {
  const valid = createDefaultWorkspace("one");
  expect(parseWorkspaceJson(serializeWorkspace(valid))).toEqual(valid);
  expect(parseSplitLayout({ type: "tabs", tabs: ["threads", "actions"], activeTabIndex: 0 })).toBeNull();
  expect(parseStoredWorkspace({ version: 2, ...valid, layout: "threads" })).toBeNull();
  expect(parseStoredWorkspace({ version: 2, ...valid, activePaneId: "thread:missing" })).toBeNull();
  expect(parseStoredWorkspace({ version: 2, ...valid, panes: [...valid.panes, valid.panes[0]] })).toBeNull();
  expect(parseWorkspaceJson("not json")).toBeNull();
});

it("preserves an intentionally empty workspace", () => {
  const empty: MosaicWorkspace = { panes: [], layout: null, activePaneId: null, actionOrigins: {} };
  expect(parseWorkspaceJson(serializeWorkspace(empty))).toEqual(empty);
});
