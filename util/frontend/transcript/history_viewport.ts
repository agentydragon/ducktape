import { type CommittedPlan, type HistoryGeometry, type LayoutPlan, mountedRange } from "./history_geometry";
import type { FollowReason, HistoryDiagnostics, HistoryLayout } from "./history_diagnostics";

type Anchor = {
  readonly entity: string;
  readonly offset: number;
  readonly inset: number;
  readonly target: HTMLElement | null;
};
type Intent =
  | { readonly kind: "following" }
  | { readonly kind: "reading"; readonly anchor: Anchor }
  | { readonly kind: "gesture"; readonly anchor: Anchor; readonly direction: "up" | "down" | "unknown" };

interface VerifiedLayout {
  readonly committed: CommittedPlan;
  readonly intentRevision: number;
  readonly maximum: number;
}

interface PendingWrite {
  readonly intentRevision: number;
  readonly top: number;
}

const close = (left: number, right: number): boolean => Math.abs(left - right) <= 1;
const clamp = (value: number, maximum: number): number => Math.max(0, Math.min(value, maximum));
const OVERSCAN_PIXELS = 256;

/** The DOM boundary: observers report changes; only reconcile() can move the viewport. */
export class HistoryViewport {
  private element: HTMLDivElement | null = null;
  private content: HTMLDivElement | null = null;
  private intent: Intent = { kind: "following" };
  private intentRevision = 0;
  private committed: CommittedPlan | null = null;
  private entities: ReadonlyMap<string, string> = new Map();
  private presentations: ReadonlyMap<string, { kind: string; entityCount: number }> = new Map();
  private nodes = new Map<string, HTMLElement>();
  private resize: ResizeObserver | null = null;
  private mutations: MutationObserver | null = null;
  private styles: MutationObserver | null = null;
  private observed = new Set<HTMLElement>();
  private seen = new Set<string>();
  private pending: PendingWrite | null = null;
  private lastTop = 0;
  private lastMaximum = 0;
  private lastAnchor: Anchor | null = null;
  private pointer = false;
  private touchY: number | null = null;
  private inCommit = false;
  private requested = false;
  private disposed = false;
  private settled = false;
  private lastTotal = 0;
  width = 0;
  height = 0;
  endVisible = true;

  constructor(
    private readonly scopeId: string,
    private readonly viewId: string,
    private readonly geometry: HistoryGeometry,
    private readonly render: (synchronous: boolean) => void,
    private readonly loadOlder: () => void,
    private readonly diagnostics: HistoryDiagnostics | undefined
  ) {}

  mount(element: HTMLDivElement, content: HTMLDivElement, tail: HTMLDivElement): void {
    this.disposed = false;
    this.requested = false;
    this.observed = new Set();
    this.element = element;
    this.content = content;
    this.resize = new ResizeObserver(() => this.refresh());
    this.resize.observe(element);
    this.resize.observe(content);
    this.resize.observe(tail);
    this.mutations = new MutationObserver((records) => {
      if (this.invalidateMutations(records)) this.request();
      else this.refresh();
    });
    this.mutations.observe(content, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["style", "class", "hidden", "open", "aria-expanded", "aria-busy"],
    });
    this.mutations.observe(tail, { subtree: true, childList: true, characterData: true, attributes: true });
    this.styles = new MutationObserver(() => this.invalidateStyle());
    this.styles.observe(document.documentElement, { attributes: true });
    this.styles.observe(document.body, { attributes: true });
    document.fonts.addEventListener("loadingdone", this.fontsChanged);
    element.addEventListener("scrollend", this.scrollEnded);
    this.publish();
  }

  dispose(): void {
    this.disposed = true;
    this.pending = null;
    this.resize?.disconnect();
    this.mutations?.disconnect();
    this.styles?.disconnect();
    document.fonts.removeEventListener("loadingdone", this.fontsChanged);
    this.element?.removeEventListener("scrollend", this.scrollEnded);
  }

  private fontsChanged = (): void => this.invalidateStyle();

  private invalidateStyle(): void {
    if (this.disposed) return;
    this.geometry.invalidate(this.committed?.plan.rows.map((row) => row.key) ?? []);
    this.request();
  }

  private request(): void {
    if (this.disposed) return;
    this.setSettled(false);
    if (this.requested) return;
    this.requested = true;
    this.render(!this.inCommit);
  }

  private invalidateMutations(records: readonly MutationRecord[]): boolean {
    if (this.disposed) return false;
    const keys = new Set<string>();
    for (const record of records) {
      const target = record.target instanceof Element ? record.target : record.target.parentElement;
      const row = target?.closest<HTMLElement>("[data-history-row-key]");
      // Row positioning is a plan output, not a change to the row's rendered contents.
      if (row && !(record.type === "attributes" && target === row)) keys.add(row.dataset.historyRowKey!);
    }
    this.geometry.invalidate(keys);
    return keys.size > 0;
  }

  private refresh(): void {
    if (this.disposed || this.inCommit || !this.committed) return;
    this.commit(this.committed.plan, this.entities, this.presentations);
  }

  range(plan: LayoutPlan, entities: ReadonlyMap<string, string>): ReturnType<typeof mountedRange> {
    const height = this.height || 600;
    let top = this.lastTop;
    const retained = new Set<string>();
    if (this.intent.kind === "following") top = Math.max(0, plan.total - height);
    else {
      const key = entities.get(this.intent.anchor.entity) ?? this.intent.anchor.entity;
      const row = plan.rows.find((candidate) => candidate.key === key);
      if (row) {
        retained.add(row.key);
        top = Math.max(0, row.start + this.intent.anchor.inset - this.intent.anchor.offset);
      }
    }
    return mountedRange(plan, top, height, OVERSCAN_PIXELS, retained);
  }

  /** A React commit is acknowledged only after its current boxes have been read. */
  commit(
    plan: LayoutPlan,
    entities: ReadonlyMap<string, string>,
    presentations: ReadonlyMap<string, { kind: string; entityCount: number }>
  ): void {
    const element = this.element;
    const content = this.content;
    if (this.disposed || !element || !content) return;
    this.inCommit = true;
    this.requested = false;
    try {
      this.entities = entities;
      this.presentations = presentations;
      this.nodes = new Map(
        [...content.querySelectorAll<HTMLElement>("[data-history-row-key]")].map((node) => [
          node.dataset.historyRowKey!,
          node,
        ])
      );
      const invalidated = this.invalidateMutations(this.mutations?.takeRecords() ?? []);
      const width = content.getBoundingClientRect().width;
      const height = element.clientHeight;
      const viewportChanged = this.width !== width || this.height !== height;
      this.width = width;
      this.height = height;
      this.committed = this.geometry.commit(plan);
      this.observeNodes();
      if (width <= 0) {
        this.setSettled(false);
        return;
      }
      if (invalidated || viewportChanged || plan.width !== width) {
        this.request();
        return;
      }
      const observations = [...this.nodes].map(([key, node]) => {
        const rect = node.getBoundingClientRect();
        return { key, height: rect.height, width: rect.width };
      });
      for (const observation of observations) {
        const row = plan.rows.find((candidate) => candidate.key === observation.key);
        if (!row) throw new Error(`Mounted history row is absent from committed plan: ${observation.key}`);
        if (row.height.kind === "estimate" || row.height.pixels !== observation.height) {
          this.diagnostics?.mark({
            kind: "measure",
            key: row.key,
            estimate: row.height.pixels,
            measured: observation.height,
            first: !this.seen.has(row.key),
            remembered: !this.seen.has(row.key) && row.height.kind === "estimate" && row.height.remembered,
          });
        }
        this.seen.add(row.key);
      }
      if (this.geometry.observe(this.committed, observations)) {
        this.request();
        return;
      }
      if (
        observations.some((observation) => {
          const row = plan.rows.find((candidate) => candidate.key === observation.key)!;
          return (
            row.height.kind !== "observed" || row.height.pixels !== observation.height || observation.width !== width
          );
        })
      ) {
        throw new Error("History geometry did not admit the current committed measurements");
      }
      if (plan.total !== this.lastTotal) {
        this.diagnostics?.mark({
          kind: "virtual-size",
          before: this.lastTotal,
          after: plan.total,
          sync: false,
          following: this.intent.kind === "following",
        });
        this.lastTotal = plan.total;
        this.diagnostics?.snapshotView("virtual-change", this.viewId, "committed-size");
      }
      this.diagnostics?.snapshotView("commit", this.viewId, "layout-verified");
      const maximum = Math.max(0, element.scrollHeight - element.clientHeight);
      this.acceptMovement(maximum);
      this.reconcile({ committed: this.committed, intentRevision: this.intentRevision, maximum });
      this.lastMaximum = Math.max(0, element.scrollHeight - element.clientHeight);
      this.updateEndVisible();
      if (this.intent.kind === "gesture" || (this.intent.kind === "reading" && !this.intent.anchor.target))
        this.rebaseAnchor();
      this.lastAnchor = this.currentAnchor();
      const busy = content.querySelector('[aria-busy="true"]') !== null;
      this.setSettled(!busy && !this.requested);
      this.maybeLoadOlder();
    } finally {
      this.inCommit = false;
    }
  }

  private observeNodes(): void {
    const current = new Set(this.nodes.values());
    for (const node of this.observed) if (!current.has(node)) this.resize?.unobserve(node);
    for (const node of current) if (!this.observed.has(node)) this.resize?.observe(node);
    this.observed = current;
  }

  private currentAnchor(target: HTMLElement | null = null): Anchor | null {
    const element = this.element;
    if (!element) return null;
    const top = element.getBoundingClientRect().top;
    const selected = target?.closest<HTMLElement>("[data-history-row-key]");
    const row = selected ?? [...this.nodes.values()].find((node) => node.getBoundingClientRect().bottom > top);
    if (!row) return null;
    const visibleTarget = target?.isConnected && target.getClientRects().length > 0 ? target : null;
    return {
      entity: row.dataset.historyRowKey!,
      offset: (visibleTarget ?? row).getBoundingClientRect().top - top,
      inset: visibleTarget ? visibleTarget.getBoundingClientRect().top - row.getBoundingClientRect().top : 0,
      target: visibleTarget,
    };
  }

  private setIntent(intent: Intent, reason: FollowReason): void {
    const wasFollowing = this.intent.kind === "following";
    this.intent = intent;
    this.intentRevision++;
    this.pending = null;
    if (wasFollowing !== (intent.kind === "following")) {
      this.diagnostics?.mark({ kind: "follow", following: intent.kind === "following", reason });
    }
    this.publish();
  }

  private rebaseAnchor(): void {
    if (this.intent.kind === "following") return;
    const anchor = this.currentAnchor();
    if (anchor) this.intent = { ...this.intent, anchor };
  }

  private acceptMovement(maximum: number): boolean {
    const element = this.element!;
    const top = element.scrollTop;
    const previous = this.lastTop;
    const owned = this.pending?.intentRevision === this.intentRevision && close(top, clamp(this.pending.top, maximum));
    const clamped = !close(previous, clamp(previous, maximum)) && close(top, clamp(previous, maximum));
    this.lastTop = top;
    if (owned) this.pending = null;
    if (owned || clamped || close(top, previous)) return false;
    const movedUp = top < previous;
    const gestureUp = this.intent.kind === "gesture" && this.intent.direction === "up";
    // Several browser clamps may precede this read while width, height and row measurements
    // change. Their intermediate maximum is not a user gesture or an anchor adjustment.
    if (
      movedUp &&
      maximum !== this.lastMaximum &&
      this.intent.kind !== "gesture" &&
      !this.pointer &&
      this.touchY === null
    )
      return false;
    // An unowned downward return uses this view's last committed extent. Unlike an arbitrary
    // historical bottom, it requires actual movement and cannot override an upward gesture.
    if (!movedUp && !gestureUp && (maximum - top <= 24 || close(top, this.lastMaximum))) {
      this.setIntent({ kind: "following" }, "scrolled-to-bottom");
      return true;
    }
    if (this.intent.kind === "following") {
      const anchor = this.lastAnchor
        ? { ...this.lastAnchor, offset: this.lastAnchor.offset - (top - previous) }
        : this.currentAnchor();
      if (anchor)
        this.setIntent(
          this.pointer || this.touchY !== null
            ? { kind: "gesture", anchor, direction: movedUp ? "up" : "down" }
            : { kind: "reading", anchor },
          "scrolled-up"
        );
    } else {
      this.intent = {
        ...this.intent,
        anchor: {
          ...this.intent.anchor,
          target: null,
          inset: 0,
          offset: this.intent.anchor.offset - this.intent.anchor.inset - (top - previous),
        },
      };
    }
    return false;
  }

  /** The only scroll-position writer; requires the current commit and intent revision. */
  private reconcile(layout: VerifiedLayout): void {
    if (this.disposed || layout.committed !== this.committed || layout.intentRevision !== this.intentRevision) return;
    const element = this.element!;
    let target = layout.maximum;
    let reason = "follow-committed-layout";
    if (this.intent.kind !== "following") {
      let anchor = this.intent.anchor;
      const row = this.nodes.get(this.entities.get(anchor.entity) ?? anchor.entity);
      const nested = anchor.target?.isConnected && anchor.target.getClientRects().length > 0 ? anchor.target : null;
      if (anchor.target && !nested) {
        anchor = { ...anchor, target: null, offset: anchor.offset - anchor.inset, inset: 0 };
        this.intent = { ...this.intent, anchor };
      }
      const node = nested ?? row;
      if (!node) {
        this.rebaseAnchor();
        return;
      }
      target =
        element.scrollTop + node.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
      reason = "restore-committed-anchor";
    }
    const reachable = clamp(target, layout.maximum);
    if (!close(reachable, element.scrollTop)) {
      this.diagnostics?.snapshotView("scroll-before", this.viewId, reason);
      element.scrollTop = reachable;
      this.pending = { intentRevision: this.intentRevision, top: element.scrollTop };
      this.lastTop = element.scrollTop;
      this.diagnostics?.snapshotView("scroll-after", this.viewId, reason);
    }
    if (this.intent.kind !== "following") {
      if (!close(target, reachable)) this.rebaseAnchor();
      else if (this.intent.anchor.target?.isConnected) {
        const row = this.nodes.get(this.entities.get(this.intent.anchor.entity) ?? this.intent.anchor.entity);
        if (row)
          this.intent = {
            ...this.intent,
            anchor: {
              ...this.intent.anchor,
              inset: this.intent.anchor.target.getBoundingClientRect().top - row.getBoundingClientRect().top,
            },
          };
      }
    }
  }

  input(source: "wheel" | "key" | "touch", direction: "up" | "down", canScroll: boolean): void {
    const element = this.element;
    if (!element) return;
    this.diagnostics?.mark({ kind: "input", source, direction, scrollTop: element.scrollTop });
    if (!canScroll) return;
    this.acceptMovement(Math.max(0, element.scrollHeight - element.clientHeight));
    // A new input can arrive after a DOM resize and before its new plan commits. Continue
    // the verified reading anchor; sampling that intermediate DOM would absorb the resize.
    const anchor = this.intent.kind === "following" ? (this.lastAnchor ?? this.currentAnchor()) : this.intent.anchor;
    if (!anchor) return;
    const reason: FollowReason = source === "wheel" ? "wheel-up" : source === "touch" ? "touch-up" : "key-up";
    this.setIntent({ kind: "gesture", anchor, direction }, reason);
  }

  pointerDown(): void {
    this.pointer = true;
    this.pending = null;
    this.intentRevision++;
    if (this.element)
      this.diagnostics?.mark({
        kind: "input",
        source: "pointer",
        direction: "unknown",
        scrollTop: this.element.scrollTop,
      });
  }

  pointerUp(): void {
    this.pointer = false;
  }

  touchStart(y: number): void {
    this.touchY = y;
  }

  touchMove(y: number): void {
    if (this.touchY !== null) this.input("touch", y > this.touchY ? "up" : "down", true);
    this.touchY = y;
  }

  touchEnd(): void {
    this.touchY = null;
  }

  key(key: string): void {
    if (key === "End") {
      this.follow();
      return;
    }
    if (!["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", " "].includes(key) || !this.element) return;
    const up = ["ArrowUp", "PageUp", "Home"].includes(key);
    this.input(
      "key",
      up ? "up" : "down",
      up ? this.element.scrollTop > 0 : this.element.scrollTop < this.element.scrollHeight - this.element.clientHeight
    );
  }

  disclose(target: HTMLElement): void {
    const element = this.element;
    if (!element) return;
    const anchor = this.currentAnchor(target);
    if (anchor && element.scrollHeight > element.clientHeight) {
      this.setIntent({ kind: "reading", anchor }, "disclosure-click");
      this.diagnostics?.mark({ kind: "anchor", key: anchor.entity, offset: anchor.offset });
    }
    this.diagnostics?.mark({
      kind: "click",
      scrollTop: element.scrollTop,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    });
  }

  scrolled(): void {
    const element = this.element;
    if (!element) return;
    const previous = this.lastTop;
    const followed = this.acceptMovement(Math.max(0, element.scrollHeight - element.clientHeight));
    this.diagnostics?.mark({
      kind: "scroll",
      scrollTop: element.scrollTop,
      previousTop: previous,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      followed,
    });
    this.updateEndVisible();
    this.request();
  }

  private scrollEnded = (): void => {
    this.diagnostics?.mark({ kind: "scrollend", restoring: false, capturing: this.intent.kind === "gesture" });
    if (this.intent.kind === "gesture") {
      this.intent = { kind: "reading", anchor: this.intent.anchor };
      this.publish();
    }
    this.maybeLoadOlder();
  };

  follow(): void {
    this.setIntent({ kind: "following" }, "jump-to-latest");
    this.request();
  }

  private maybeLoadOlder(): void {
    if (
      this.element &&
      this.settled &&
      this.committed?.plan.rows.length &&
      this.element.scrollTop < this.element.clientHeight
    )
      this.loadOlder();
  }

  private updateEndVisible(): void {
    if (!this.element) return;
    const visible = this.element.scrollHeight - this.element.scrollTop - this.element.clientHeight <= 24;
    if (this.endVisible !== visible) {
      this.endVisible = visible;
      this.request();
    }
  }

  private setSettled(settled: boolean): void {
    if (this.settled !== settled) this.diagnostics?.mark({ kind: "settled", settled });
    this.settled = settled;
    this.publish();
  }

  private publish(): void {
    if (!this.element) return;
    this.element.dataset.scrollMode = this.intent.kind === "following" ? "following" : "reading";
    this.element.dataset.layoutSettled = String(this.settled);
  }

  snapshot(): HistoryLayout | null {
    const element = this.element;
    const content = this.content;
    const plan = this.committed?.plan;
    if (!element || !content || !plan) return null;
    const top = element.getBoundingClientRect().top;
    return {
      scopeId: this.scopeId,
      viewId: this.viewId,
      following: this.intent.kind === "following",
      restoring: false,
      capturing: this.intent.kind === "gesture",
      rowCount: plan.rows.length,
      virtualTotal: plan.total,
      viewport: {
        top,
        height: element.clientHeight,
        width: element.clientWidth,
        scrollTop: element.scrollTop,
        scrollHeight: element.scrollHeight,
      },
      content: { height: content.getBoundingClientRect().height, scrollHeight: content.scrollHeight },
      plannedRows: plan.rows.slice(0, 128).map((row) => ({ key: row.key, ...this.presentations.get(row.key)! })),
      virtualRows: plan.rows.slice(0, 128).map((row) => ({
        index: row.index,
        key: row.key,
        start: row.start,
        size: row.height.pixels,
        measured: row.height.kind === "observed" ? row.height.pixels : null,
        estimate: row.height.kind === "estimate" ? row.height.pixels : null,
        remembered: row.height.kind === "estimate" && row.height.remembered,
      })),
      mountedRows: [...this.nodes].slice(0, 128).map(([key, node]) => {
        const rect = node.getBoundingClientRect();
        return {
          index: Number(node.dataset.index),
          key,
          top: rect.top - top,
          height: rect.height,
          width: rect.width,
          busy: node.querySelector('[aria-busy="true"]') !== null,
        };
      }),
    };
  }
}
