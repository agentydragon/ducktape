import type { SerializedDockview } from "dockview-react";
import { z } from "zod";

/** What a pane shows. Browser panes are singletons; thread and action panes are one per subject. */
export type DockPane =
  | { kind: "threads" }
  | { kind: "actions" }
  | { kind: "history" }
  | { kind: "thread"; threadId: string }
  | { kind: "action"; requestId: string };

const DockPaneSchema: z.ZodType<DockPane> = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("threads") }),
  z.object({ kind: z.literal("actions") }),
  z.object({ kind: z.literal("history") }),
  z.object({ kind: z.literal("thread"), threadId: z.string().min(1) }),
  z.object({ kind: z.literal("action"), requestId: z.string().min(1) }),
]);

export function paneId(pane: DockPane): string {
  switch (pane.kind) {
    case "thread":
      return `thread:${pane.threadId}`;
    case "action":
      return `action:${pane.requestId}`;
    default:
      return pane.kind;
  }
}

/** Browser panes sit in the left column; everything opened from them shares the content group. */
export function isContentPane(pane: DockPane): boolean {
  return pane.kind === "thread" || pane.kind === "action" || pane.kind === "history";
}

/** The stored layout is dockview's own serialization, which `fromJSON` validates when it is applied. */
const SerializedDockviewSchema = z.custom<SerializedDockview>(
  (value) => typeof value === "object" && value !== null && "grid" in value && "panels" in value
);

const STORED_VERSION = 1 as const;

const StoredWorkspaceSchema: z.ZodType<{
  version: typeof STORED_VERSION;
  panes: DockPane[];
  activePaneId: string | null;
  layout: SerializedDockview | null;
}> = z.object({
  version: z.literal(STORED_VERSION),
  panes: z.array(DockPaneSchema),
  activePaneId: z.string().nullable(),
  layout: SerializedDockviewSchema.nullable(),
});

export interface DockWorkspace {
  panes: readonly DockPane[];
  /** The pane a phone shows, and the one focused on desktop. */
  activePaneId: string | null;
  /** Desktop geometry; `null` until the first layout, then restored only if it names exactly `panes`. */
  layout: SerializedDockview | null;
}

export const DEFAULT_PANES: readonly DockPane[] = [{ kind: "threads" }, { kind: "actions" }];

export function defaultWorkspace(): DockWorkspace {
  return { panes: DEFAULT_PANES, activePaneId: paneId(DEFAULT_PANES[0]), layout: null };
}

/** Adds `pane` unless one with its id is open, and activates it. */
export function openPane(workspace: DockWorkspace, pane: DockPane): DockWorkspace {
  const id = paneId(pane);
  const open = workspace.panes.some((candidate) => paneId(candidate) === id);
  return { ...workspace, panes: open ? workspace.panes : [...workspace.panes, pane], activePaneId: id };
}

/** Removes the pane; when it was the active one the previous pane in the list takes over. */
export function closePane(workspace: DockWorkspace, id: string): DockWorkspace {
  const index = workspace.panes.findIndex((pane) => paneId(pane) === id);
  if (index < 0) return workspace;
  const panes = workspace.panes.filter((_, position) => position !== index);
  const activePaneId =
    workspace.activePaneId === id
      ? panes.length === 0
        ? null
        : paneId(panes[Math.max(0, index - 1)])
      : workspace.activePaneId;
  return { ...workspace, panes, activePaneId };
}

function hasUniqueIds(panes: readonly DockPane[]): boolean {
  const ids = panes.map(paneId);
  return new Set(ids).size === ids.length;
}

/** A layout restores only if it holds exactly the stored panes: dockview would otherwise show a
 * panel for a pane that is gone, or none for one that is open. */
function layoutMatchesPanes(layout: SerializedDockview, panes: readonly DockPane[]): boolean {
  const layoutIds = new Set(Object.keys(layout.panels));
  return layoutIds.size === panes.length && panes.every((pane) => layoutIds.has(paneId(pane)));
}

export function parseStoredWorkspace(raw: string | null): DockWorkspace | null {
  if (raw === null) return null;
  try {
    const stored = StoredWorkspaceSchema.parse(JSON.parse(raw));
    if (!hasUniqueIds(stored.panes)) throw new Error("duplicate pane ids");
    if (stored.activePaneId !== null && !stored.panes.some((pane) => paneId(pane) === stored.activePaneId)) {
      throw new Error(`active pane ${stored.activePaneId} is not open`);
    }
    return {
      panes: stored.panes,
      activePaneId: stored.activePaneId,
      layout: stored.layout !== null && layoutMatchesPanes(stored.layout, stored.panes) ? stored.layout : null,
    };
  } catch (reason: unknown) {
    console.warn("dock: ignoring saved workspace", reason);
    return null;
  }
}

export function serializeWorkspace(workspace: DockWorkspace): string {
  return JSON.stringify({ version: STORED_VERSION, ...workspace });
}

const STORAGE_KEY = "agentplane-dock-workspace";

export function loadWorkspace(): DockWorkspace {
  try {
    return parseStoredWorkspace(window.localStorage.getItem(STORAGE_KEY)) ?? defaultWorkspace();
  } catch (reason: unknown) {
    console.warn("dock: failed to read saved workspace", reason);
    return defaultWorkspace();
  }
}

export function saveWorkspace(workspace: DockWorkspace): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, serializeWorkspace(workspace));
  } catch (reason: unknown) {
    console.warn("dock: failed to persist workspace", reason);
  }
}
