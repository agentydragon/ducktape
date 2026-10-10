/** User-owned pane arrangement for the experimental mosaic view. */
export type MosaicPane =
  { kind: "thread"; id: string; threadId: string } | { kind: "action"; id: string; requestId: string };

export type MosaicDockEdge = "left" | "right" | "top" | "bottom";

export type MosaicLayoutNode =
  | { type: "pane"; paneId: string }
  | {
      type: "split";
      id: string;
      direction: "horizontal" | "vertical";
      ratio: number;
      first: MosaicLayoutNode;
      second: MosaicLayoutNode;
    };

export interface MosaicWorkspace {
  panes: MosaicPane[];
  layout: MosaicLayoutNode | null;
  activePaneId: string | null;
}

interface StoredMosaicWorkspace extends MosaicWorkspace {
  version: 1;
}

const MIN_SPLIT_RATIO = 0.15;
const MAX_SPLIT_RATIO = 0.85;

export function clampSplitRatio(ratio: number): number {
  return Math.min(MAX_SPLIT_RATIO, Math.max(MIN_SPLIT_RATIO, ratio));
}

export function paneIds(node: MosaicLayoutNode | null): string[] {
  if (node === null) return [];
  if (node.type === "pane") return [node.paneId];
  return [...paneIds(node.first), ...paneIds(node.second)];
}

export function createDefaultLayout(ids: readonly string[]): MosaicLayoutNode | null {
  if (ids.length === 0) return null;
  if (ids.length === 1) return { type: "pane", paneId: ids[0] };

  // Keep the first thread full-height, with the second thread and first action stacked alongside.
  if (ids.length === 3) {
    return {
      type: "split",
      id: "mosaic-split-1",
      direction: "horizontal",
      ratio: 0.5,
      first: { type: "pane", paneId: ids[0] },
      second: {
        type: "split",
        id: "mosaic-split-2",
        direction: "vertical",
        ratio: 0.5,
        first: { type: "pane", paneId: ids[1] },
        second: { type: "pane", paneId: ids[2] },
      },
    };
  }

  const middle = Math.ceil(ids.length / 2);
  return {
    type: "split",
    id: "mosaic-split-1",
    direction: "horizontal",
    ratio: 0.5,
    first: createBalancedLayout(ids.slice(0, middle), 2),
    second: createBalancedLayout(ids.slice(middle), 3),
  };
}

function createBalancedLayout(ids: readonly string[], splitNumber: number): MosaicLayoutNode {
  if (ids.length === 1) return { type: "pane", paneId: ids[0] };
  const middle = Math.ceil(ids.length / 2);
  return {
    type: "split",
    id: `mosaic-split-${splitNumber}`,
    direction: splitNumber % 2 === 0 ? "vertical" : "horizontal",
    ratio: 0.5,
    first: createBalancedLayout(ids.slice(0, middle), splitNumber * 2),
    second: createBalancedLayout(ids.slice(middle), splitNumber * 2 + 1),
  };
}

export function removePaneFromLayout(node: MosaicLayoutNode | null, paneId: string): MosaicLayoutNode | null {
  if (node === null) return null;
  if (node.type === "pane") return node.paneId === paneId ? null : node;

  const first = removePaneFromLayout(node.first, paneId);
  const second = removePaneFromLayout(node.second, paneId);
  if (first === null) return second;
  if (second === null) return first;
  return { ...node, first, second };
}

function nextSplitId(node: MosaicLayoutNode | null): string {
  const ids = new Set<string>();
  function collect(current: MosaicLayoutNode | null): void {
    if (current === null || current.type === "pane") return;
    ids.add(current.id);
    collect(current.first);
    collect(current.second);
  }
  collect(node);

  let number = 1;
  while (ids.has(`mosaic-split-${number}`)) number += 1;
  return `mosaic-split-${number}`;
}

function insertBeside(
  node: MosaicLayoutNode | null,
  sourcePaneId: string,
  targetPaneId: string,
  edge: MosaicDockEdge,
  splitId: string
): MosaicLayoutNode | null {
  if (node === null) return null;
  if (node.type === "pane") {
    if (node.paneId !== targetPaneId) return node;
    const source = { type: "pane", paneId: sourcePaneId } as const;
    const sourceFirst = edge === "left" || edge === "top";
    return {
      type: "split",
      id: splitId,
      direction: edge === "left" || edge === "right" ? "horizontal" : "vertical",
      ratio: 0.5,
      first: sourceFirst ? source : node,
      second: sourceFirst ? node : source,
    };
  }

  const first = insertBeside(node.first, sourcePaneId, targetPaneId, edge, splitId);
  if (first !== node.first) return { ...node, first: first ?? node.first };
  const second = insertBeside(node.second, sourcePaneId, targetPaneId, edge, splitId);
  return second === node.second ? node : { ...node, second: second ?? node.second };
}

/** Remove a pane from its old position, then place it on an edge of the target pane. */
export function dockPane(
  layout: MosaicLayoutNode | null,
  sourcePaneId: string,
  targetPaneId: string,
  edge: MosaicDockEdge
): MosaicLayoutNode | null {
  if (sourcePaneId === targetPaneId || !paneIds(layout).includes(targetPaneId)) return layout;
  const withoutSource = removePaneFromLayout(layout, sourcePaneId);
  const splitId = nextSplitId(withoutSource);
  return insertBeside(withoutSource, sourcePaneId, targetPaneId, edge, splitId);
}

export function resizeSplit(node: MosaicLayoutNode | null, splitId: string, ratio: number): MosaicLayoutNode | null {
  if (node === null || node.type === "pane") return node;
  if (node.id === splitId) return { ...node, ratio: clampSplitRatio(ratio) };
  return {
    ...node,
    first: resizeSplit(node.first, splitId, ratio) ?? node.first,
    second: resizeSplit(node.second, splitId, ratio) ?? node.second,
  };
}

function parsePane(value: unknown): MosaicPane | null {
  if (typeof value !== "object" || value === null) return null;
  const pane = value as Record<string, unknown>;
  if (pane.kind === "thread" && typeof pane.threadId === "string" && pane.id === `thread:${pane.threadId}`) {
    return { kind: "thread", id: pane.id, threadId: pane.threadId };
  }
  if (pane.kind === "action" && typeof pane.requestId === "string" && pane.id === `action:${pane.requestId}`) {
    return { kind: "action", id: pane.id, requestId: pane.requestId };
  }
  return null;
}

function parseLayout(value: unknown): MosaicLayoutNode | null {
  if (typeof value !== "object" || value === null) return null;
  const node = value as Record<string, unknown>;
  if (node.type === "pane" && typeof node.paneId === "string") return { type: "pane", paneId: node.paneId };
  if (
    node.type !== "split" ||
    typeof node.id !== "string" ||
    (node.direction !== "horizontal" && node.direction !== "vertical") ||
    typeof node.ratio !== "number" ||
    !Number.isFinite(node.ratio)
  ) {
    return null;
  }
  const first = parseLayout(node.first);
  const second = parseLayout(node.second);
  if (first === null || second === null) return null;
  return {
    type: "split",
    id: node.id,
    direction: node.direction,
    ratio: clampSplitRatio(node.ratio),
    first,
    second,
  };
}

/** Parse and validate browser storage. Invalid or stale data falls back to the seeded layout. */
export function parseStoredWorkspace(value: unknown): MosaicWorkspace | null {
  if (typeof value !== "object" || value === null) return null;
  const stored = value as Record<string, unknown>;
  if (stored.version !== 1 || !Array.isArray(stored.panes)) return null;
  const panes = stored.panes.map(parsePane);
  if (panes.some((pane) => pane === null)) return null;
  const parsedPanes = panes as MosaicPane[];
  const ids = parsedPanes.map((pane) => pane.id);
  if (new Set(ids).size !== ids.length) return null;

  const layout = stored.layout === null ? null : parseLayout(stored.layout);
  if (stored.layout !== null && layout === null) return null;
  const layoutIds = paneIds(layout);
  if (new Set(layoutIds).size !== layoutIds.length) return null;
  const splitIds = new Set<string>();
  function collectSplitIds(node: MosaicLayoutNode | null): boolean {
    if (node === null || node.type === "pane") return true;
    if (splitIds.has(node.id)) return false;
    splitIds.add(node.id);
    return collectSplitIds(node.first) && collectSplitIds(node.second);
  }
  if (!collectSplitIds(layout)) return null;
  if (layoutIds.length !== ids.length || ids.some((id) => !layoutIds.includes(id))) return null;
  const activePaneId = stored.activePaneId;
  if (activePaneId !== null && (typeof activePaneId !== "string" || !ids.includes(activePaneId))) return null;
  return { panes: parsedPanes, layout, activePaneId };
}

export function serializeWorkspace(workspace: MosaicWorkspace): string {
  const stored: StoredMosaicWorkspace = { version: 1, ...workspace };
  return JSON.stringify(stored);
}

export function parseWorkspaceJson(raw: string | null): MosaicWorkspace | null {
  if (raw === null) return null;
  try {
    return parseStoredWorkspace(JSON.parse(raw) as unknown);
  } catch {
    return null;
  }
}
