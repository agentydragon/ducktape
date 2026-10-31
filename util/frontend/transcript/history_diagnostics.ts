/** Why the history started or stopped following the tail. */
export type FollowReason =
  | "jump-to-latest"
  | "returned-to-previous-bottom"
  | "scrolled-to-bottom"
  | "scrolled-up"
  | "wheel-up"
  | "key-up"
  | "touch-up"
  | "disclosure-click";

export type HistoryEvent =
  | { kind: "follow"; following: boolean; reason: FollowReason }
  | {
      kind: "scroll";
      scrollTop: number;
      previousTop: number;
      scrollHeight: number;
      clientHeight: number;
      followed: boolean;
    }
  | {
      kind: "input";
      source: "wheel" | "key" | "touch" | "pointer";
      direction: "up" | "down" | "unknown";
      scrollTop: number;
    }
  | { kind: "virtual-size"; before: number; after: number; sync: boolean; following: boolean }
  | { kind: "scrollend"; restoring: boolean; capturing: boolean }
  /** The history's content or tail changed size. `pinned`: it was followed to the bottom. */
  | { kind: "resize"; scrollTop: number; scrollHeight: number; pinned: boolean }
  | { kind: "click"; scrollTop: number; scrollHeight: number; clientHeight: number }
  | { kind: "anchor"; key: string; offset: number }
  | { kind: "restore"; key: string; correction: number | null }
  | { kind: "prepend"; added: number }
  | { kind: "marker" }
  | { kind: "load-older" }
  | { kind: "older-state"; loading: boolean; available: boolean; rowCount: number }
  /** A row's height was read. `estimate` is what the virtualizer laid it out with before: a
   * remembered reading of an earlier visit if `remembered`, else a flat guess. */
  | { kind: "measure"; key: string; estimate: number; measured: number; first: boolean; remembered: boolean }
  | { kind: "settled"; settled: boolean };

/** Geometry and identifiers only; never message text, arguments, or output. */
export interface HistoryLayout {
  scopeId: string;
  viewId: string;
  following: boolean;
  restoring: boolean;
  capturing: boolean;
  rowCount: number;
  virtualTotal: number;
  viewport: { top: number; height: number; width: number; scrollTop: number; scrollHeight: number };
  content: { height: number; scrollHeight: number };
  plannedRows: { key: string; kind: string; entityCount: number }[];
  virtualRows: {
    index: number;
    key: string;
    start: number;
    size: number;
    measured: number | null;
    estimate: number | null;
    remembered: boolean;
  }[];
  mountedRows: { index: number; key: string; top: number; height: number; width: number; busy: boolean }[];
}

export type SnapshotPhase =
  "start" | "mount" | "unmount" | "commit" | "frame" | "virtual-change" | "scroll-before" | "scroll-after";
export interface HistoryDiagnostics {
  mark(event: HistoryEvent): void;
  snapshotView(phase: SnapshotPhase, viewId: string, reason: string): void;
  register(viewId: string, source: () => HistoryLayout | null): () => void;
}
