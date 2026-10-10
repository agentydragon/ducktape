import type { MosaicNode } from "react-mosaic-component";

/** Identifiers are stable across live updates and browser-local layout restoration. */
export type MosaicPane =
  | { kind: "threads"; id: "threads" }
  | { kind: "actions"; id: "actions" }
  | { kind: "history"; id: "history" }
  | { kind: "thread"; id: string; threadId: string }
  | { kind: "action"; id: string; requestId: string };

export interface MosaicWorkspace {
  panes: MosaicPane[];
  layout: MosaicNode<string> | null;
  activePaneId: string | null;
  actionOrigins: Record<string, string>;
}

const THREADS: MosaicPane = { kind: "threads", id: "threads" };
const ACTIONS: MosaicPane = { kind: "actions", id: "actions" };

export function paneIds(node: MosaicNode<string> | null): string[] {
  if (node === null) return [];
  if (typeof node === "string") return [node];
  if (node.type === "tabs") return node.tabs;
  return node.children.flatMap(paneIds);
}

/** The browser and compact queue occupy the left column; a thread is content on the right. */
export function createDefaultWorkspace(threadId: string | null): MosaicWorkspace {
  const thread: MosaicPane | null = threadId === null ? null : { kind: "thread", id: `thread:${threadId}`, threadId };
  const browsers: MosaicNode<string> = {
    type: "split",
    direction: "column",
    children: [THREADS.id, ACTIONS.id],
    splitPercentages: [65, 35],
  };
  return {
    panes: thread === null ? [THREADS, ACTIONS] : [THREADS, ACTIONS, thread],
    layout:
      thread === null
        ? browsers
        : { type: "split", direction: "row", children: [browsers, thread.id], splitPercentages: [35, 65] },
    activePaneId: thread?.id ?? THREADS.id,
    actionOrigins: {},
  };
}

/** Insert a new tile to the right of the active tile without implementing drag docking ourselves. */
export function insertPane(
  layout: MosaicNode<string> | null,
  anchor: string | null,
  added: string
): MosaicNode<string> {
  if (layout === null) return added;
  if (layout === anchor) {
    return { type: "split", direction: "row", children: [layout, added], splitPercentages: [50, 50] };
  }
  if (typeof layout === "string" || layout.type === "tabs") {
    return { type: "split", direction: "row", children: [layout, added], splitPercentages: [50, 50] };
  }
  const children = layout.children.map((child) =>
    paneIds(child).includes(anchor ?? "") ? insertPane(child, anchor, added) : child
  );
  if (children.every((child, index) => child === layout.children[index])) {
    return { type: "split", direction: "row", children: [layout, added], splitPercentages: [50, 50] };
  }
  return { ...layout, children };
}

export function removePane(layout: MosaicNode<string> | null, target: string): MosaicNode<string> | null {
  if (layout === null || layout === target) return null;
  if (typeof layout === "string" || layout.type === "tabs") return layout;
  const percentages = layout.splitPercentages ?? layout.children.map(() => 100 / layout.children.length);
  const remaining = layout.children.flatMap((child, index) => {
    const next = removePane(child, target);
    return next === null ? [] : [{ node: next, percentage: percentages[index] ?? 0 }];
  });
  if (remaining.length === 0) return null;
  if (remaining.length === 1) return remaining[0].node;
  const total = remaining.reduce((sum, item) => sum + item.percentage, 0);
  return {
    ...layout,
    children: remaining.map((item) => item.node),
    splitPercentages: remaining.map((item) => (item.percentage / total) * 100),
  };
}

function parsePane(value: unknown): MosaicPane | null {
  if (typeof value !== "object" || value === null) return null;
  const pane = value as Record<string, unknown>;
  if (pane.kind === "threads" && pane.id === "threads") return THREADS;
  if (pane.kind === "actions" && pane.id === "actions") return ACTIONS;
  if (pane.kind === "history" && pane.id === "history") return { kind: "history", id: "history" };
  if (pane.kind === "thread" && typeof pane.threadId === "string" && pane.id === `thread:${pane.threadId}`) {
    return { kind: "thread", id: pane.id, threadId: pane.threadId };
  }
  if (pane.kind === "action" && typeof pane.requestId === "string" && pane.id === `action:${pane.requestId}`) {
    return { kind: "action", id: pane.id, requestId: pane.requestId };
  }
  return null;
}

/** Mosaic v7 can make tab groups; this experimental workspace accepts split trees only. */
export function parseSplitLayout(value: unknown): MosaicNode<string> | null {
  if (typeof value === "string") return value;
  if (typeof value !== "object" || value === null) return null;
  const node = value as Record<string, unknown>;
  if (
    node.type !== "split" ||
    (node.direction !== "row" && node.direction !== "column") ||
    !Array.isArray(node.children) ||
    node.children.length < 2 ||
    (node.splitPercentages !== undefined &&
      (!Array.isArray(node.splitPercentages) ||
        node.splitPercentages.length !== node.children.length ||
        !node.splitPercentages.every((value) => typeof value === "number" && Number.isFinite(value) && value > 0)))
  ) {
    return null;
  }
  const children = node.children.map(parseSplitLayout);
  if (children.some((child) => child === null)) return null;
  const percentages = (node.splitPercentages as number[] | undefined) ?? children.map(() => 100 / children.length);
  const total = percentages.reduce((sum, item) => sum + item, 0);
  return {
    type: "split",
    direction: node.direction,
    children: children as MosaicNode<string>[],
    splitPercentages: percentages.map((item) => (item / total) * 100),
  };
}

function migrateHandRolledLayout(value: unknown): MosaicNode<string> | null {
  if (typeof value !== "object" || value === null) return null;
  const node = value as Record<string, unknown>;
  if (node.type === "pane" && typeof node.paneId === "string") return node.paneId;
  if (
    node.type !== "split" ||
    (node.direction !== "horizontal" && node.direction !== "vertical") ||
    typeof node.ratio !== "number" ||
    !Number.isFinite(node.ratio) ||
    node.ratio <= 0 ||
    node.ratio >= 1
  ) {
    return null;
  }
  const first = migrateHandRolledLayout(node.first);
  const second = migrateHandRolledLayout(node.second);
  if (first === null || second === null) return null;
  return {
    type: "split",
    direction: node.direction === "horizontal" ? "row" : "column",
    children: [first, second],
    splitPercentages: [node.ratio * 100, (1 - node.ratio) * 100],
  };
}

export function parseStoredWorkspace(value: unknown): MosaicWorkspace | null {
  if (typeof value !== "object" || value === null) return null;
  const stored = value as Record<string, unknown>;
  if ((stored.version !== 1 && stored.version !== 2) || !Array.isArray(stored.panes)) return null;
  const panes = stored.panes.map(parsePane);
  if (panes.some((pane) => pane === null)) return null;
  const parsedPanes = panes as MosaicPane[];
  const ids = parsedPanes.map((pane) => pane.id);
  if (new Set(ids).size !== ids.length) return null;
  const layout =
    stored.layout === null
      ? null
      : stored.version === 1
        ? migrateHandRolledLayout(stored.layout)
        : parseSplitLayout(stored.layout);
  if (stored.layout !== null && layout === null) return null;
  const layoutIds = paneIds(layout);
  if (
    layoutIds.length !== ids.length ||
    new Set(layoutIds).size !== ids.length ||
    ids.some((id) => !layoutIds.includes(id))
  ) {
    return null;
  }
  const activePaneId = stored.activePaneId;
  if (activePaneId !== null && (typeof activePaneId !== "string" || !ids.includes(activePaneId))) return null;
  const rawOrigins = stored.version === 2 ? stored.actionOrigins : {};
  if (typeof rawOrigins !== "object" || rawOrigins === null || Array.isArray(rawOrigins)) return null;
  const actionOrigins: Record<string, string> = {};
  for (const [paneId, origin] of Object.entries(rawOrigins)) {
    if (!ids.includes(paneId) || typeof origin !== "string" || !ids.includes(origin)) return null;
    actionOrigins[paneId] = origin;
  }
  return { panes: parsedPanes, layout, activePaneId: activePaneId as string | null, actionOrigins };
}

export function parseWorkspaceJson(raw: string | null): MosaicWorkspace | null {
  if (raw === null) return null;
  try {
    return parseStoredWorkspace(JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}

export function serializeWorkspace(workspace: MosaicWorkspace): string {
  return JSON.stringify({ version: 2, ...workspace });
}
