// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HistoryGeometry, type LayoutPlan } from "./history_geometry";
import { HistoryViewport } from "./history_viewport";
import { rememberRowHeight } from "./history_sizes";

class ControlledResizeObserver implements ResizeObserver {
  static readonly instances: ControlledResizeObserver[] = [];
  readonly observed = new Set<Element>();
  disconnected = false;

  constructor(private readonly callback: ResizeObserverCallback) {
    ControlledResizeObserver.instances.push(this);
  }

  observe(target: Element): void {
    this.observed.add(target);
  }

  unobserve(target: Element): void {
    this.observed.delete(target);
  }

  disconnect(): void {
    this.disconnected = true;
    this.observed.clear();
  }

  deliverDelayed(): void {
    this.callback([], this);
  }
}

class ControlledMutationObserver implements MutationObserver {
  static readonly instances: ControlledMutationObserver[] = [];
  readonly observed = new Set<Node>();
  disconnected = false;

  constructor(private readonly callback: MutationCallback) {
    ControlledMutationObserver.instances.push(this);
  }

  observe(target: Node): void {
    this.observed.add(target);
  }

  disconnect(): void {
    this.disconnected = true;
    this.observed.clear();
  }

  takeRecords(): MutationRecord[] {
    return [];
  }

  deliverDelayed(records: readonly MutationRecord[]): void {
    this.callback([...records], this);
  }
}

interface TestRow {
  readonly key: string;
  revision: string;
  height: number;
  readonly node: HTMLDivElement;
}

const rect = (top: number, width: number, height: number): DOMRect =>
  ({
    x: 0,
    y: top,
    top,
    right: width,
    bottom: top + height,
    left: 0,
    width,
    height,
    toJSON: () => ({}),
  }) as DOMRect;

let nextThreadId = 0;
const activeRigs = new Set<ViewportRig>();

class ViewportRig {
  readonly threadId = `history-viewport-test:${nextThreadId++}`;
  readonly geometry = new HistoryGeometry(this.threadId);
  readonly viewport = document.createElement("div");
  readonly content = document.createElement("div");
  readonly tail = document.createElement("div");
  readonly rows: TestRow[];
  readonly writes: number[] = [];
  readonly renderRequests: boolean[] = [];
  readonly controller: HistoryViewport;
  private readonly presentations: ReadonlyMap<string, { kind: string; entityCount: number }>;
  private scrollTopValue = 0;
  private widthValue: number;
  private readonly viewportHeight: number;

  constructor(
    rows: readonly { readonly key: string; readonly height: number }[],
    width: number,
    viewportHeight: number
  ) {
    this.widthValue = width;
    this.viewportHeight = viewportHeight;
    this.content.style.position = "relative";
    this.rows = rows.map(({ key, height }, index) => {
      const node = document.createElement("div");
      node.dataset.historyRowKey = key;
      node.dataset.index = String(index);
      this.content.append(node);
      return { key, revision: "revision-1", height, node };
    });
    this.viewport.append(this.content, this.tail);
    document.body.append(this.viewport);

    Object.defineProperties(this.viewport, {
      clientHeight: { configurable: true, get: () => this.viewportHeight },
      clientWidth: { configurable: true, get: () => this.widthValue },
      scrollHeight: { configurable: true, get: () => this.scrollExtent() },
      scrollTop: {
        configurable: true,
        get: () => Math.min(this.scrollTopValue, this.maximumScrollTop()),
        set: (value: number) => {
          this.writes.push(value);
          this.scrollTopValue = Math.max(0, Math.min(value, this.maximumScrollTop()));
        },
      },
      getBoundingClientRect: { configurable: true, value: () => rect(0, this.widthValue, this.viewportHeight) },
    });
    Object.defineProperty(this.content, "getBoundingClientRect", {
      configurable: true,
      value: () => rect(0, this.widthValue, this.contentHeight()),
    });
    Object.defineProperty(this.tail, "getBoundingClientRect", {
      configurable: true,
      value: () => rect(this.contentHeight(), this.widthValue, 0),
    });
    for (const row of this.rows) {
      Object.defineProperty(row.node, "getBoundingClientRect", {
        configurable: true,
        value: () => rect(this.rowTop(row) - this.viewport.scrollTop, this.widthValue, row.height),
      });
    }

    const presentations = new Map<string, { kind: string; entityCount: number }>();
    for (const { key } of this.rows) presentations.set(key, { kind: "entity", entityCount: 1 });
    this.presentations = presentations;
    this.controller = new HistoryViewport(
      this.threadId,
      `view-${this.threadId}`,
      this.geometry,
      (synchronous) => this.renderRequests.push(synchronous),
      () => undefined
    );
    activeRigs.add(this);
  }

  get resizeObserver(): ControlledResizeObserver {
    const observer = ControlledResizeObserver.instances.find((candidate) => candidate.observed.has(this.viewport));
    if (!observer) throw new Error("The viewport has no resize observer");
    return observer;
  }

  get contentObserver(): ControlledMutationObserver {
    const observer = ControlledMutationObserver.instances.find((candidate) => candidate.observed.has(this.content));
    if (!observer) throw new Error("The content has no mutation observer");
    return observer;
  }

  descriptors(): readonly { readonly key: string; readonly revision: string }[] {
    return this.rows.map(({ key, revision }) => ({ key, revision }));
  }

  mount(): void {
    this.controller.mount(this.viewport, this.content, this.tail);
  }

  commitCurrentPlan(): LayoutPlan {
    const plan = this.geometry.plan(this.descriptors(), this.controller.width);
    this.content.style.height = `${plan.total}px`;
    for (const row of plan.rows) {
      const node = this.rows.find((candidate) => candidate.key === row.key)!.node;
      node.style.top = `${row.start}px`;
    }
    const entities = new Map<string, string>();
    for (const { key } of this.rows) entities.set(key, key);
    this.controller.commit(plan, entities, this.presentations);
    return plan;
  }

  flush(maxCommits = 8): LayoutPlan {
    for (let index = 0; index < maxCommits; index++) {
      this.renderRequests.length = 0;
      const plan = this.commitCurrentPlan();
      if (this.renderRequests.length === 0) return plan;
    }
    throw new Error(`HistoryViewport did not settle in ${maxCommits} commits`);
  }

  setWidth(width: number): void {
    this.widthValue = width;
  }

  setHeight(key: string, height: number): void {
    this.rows.find((row) => row.key === key)!.height = height;
  }

  setRevision(key: string, revision: string): void {
    this.rows.find((row) => row.key === key)!.revision = revision;
  }

  userScrollTo(top: number): void {
    this.scrollTopValue = Math.max(0, Math.min(top, this.maximumScrollTop()));
  }

  dispose(): void {
    this.controller.dispose();
    activeRigs.delete(this);
  }

  private contentHeight(): number {
    return Number.parseFloat(this.content.style.height) || 0;
  }

  private rowTop(row: TestRow): number {
    return Number.parseFloat(row.node.style.top) || 0;
  }

  private scrollExtent(): number {
    const lastRowBottom = Math.max(0, ...this.rows.map((row) => this.rowTop(row) + row.height));
    return Math.max(this.contentHeight(), lastRowBottom);
  }

  private maximumScrollTop(): number {
    return Math.max(0, this.scrollExtent() - this.viewportHeight);
  }
}

beforeEach(() => {
  ControlledResizeObserver.instances.length = 0;
  ControlledMutationObserver.instances.length = 0;
  vi.stubGlobal("ResizeObserver", ControlledResizeObserver);
  vi.stubGlobal("MutationObserver", ControlledMutationObserver);
});

afterEach(() => {
  for (const rig of [...activeRigs]) rig.dispose();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe("HistoryViewport DOM boundary", () => {
  it("does not follow inaccurate estimates and settles equal and changed readings once", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `short:${index}`, height: 35 })),
      400,
      260
    );
    rememberRowHeight(rig.threadId, 400, "short:0", 35);
    rememberRowHeight(rig.threadId, 400, "short:1", 260);

    rig.mount();
    rig.commitCurrentPlan();
    const measuredEstimatePlan = rig.commitCurrentPlan();

    expect(measuredEstimatePlan.rows[0].height).toEqual({ kind: "estimate", pixels: 35, remembered: true });
    expect(measuredEstimatePlan.rows[1].height).toEqual({ kind: "estimate", pixels: 260, remembered: true });
    expect(rig.geometry.plan(rig.descriptors(), 400).rows.map((row) => row.height)).toEqual(
      Array.from({ length: 7 }, () => ({ kind: "observed", pixels: 35 }))
    );
    expect(rig.writes).toEqual([]);

    const settled = rig.flush();
    expect(settled.total).toBe(245);
    expect(settled.rows[0].height).toEqual({ kind: "observed", pixels: 35 });
    expect(rig.viewport.scrollTop).toBe(0);
    expect(rig.writes).toEqual([]);

    rig.commitCurrentPlan();
    expect(rig.writes).toEqual([]);
  });

  it("follows growth at the bottom but keeps an upward reader anchor steady", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `tall:${index}`, height: 100 })),
      400,
      200
    );
    rig.mount();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(500);
    expect(rig.writes).toEqual([500]);

    rig.setHeight("tall:6", 130);
    rig.resizeObserver.deliverDelayed();
    expect(rig.writes).toEqual([500]);
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(530);
    expect(rig.writes).toEqual([500, 530]);

    rig.controller.pointerDown();
    rig.userScrollTo(250);
    rig.controller.scrolled();
    rig.controller.pointerUp();
    rig.flush();
    expect(rig.viewport.dataset.scrollMode).toBe("reading");
    expect(rig.viewport.scrollTop).toBe(250);

    rig.setHeight("tall:6", 170);
    rig.resizeObserver.deliverDelayed();
    expect(rig.writes).toEqual([500, 530]);
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(250);
    expect(rig.viewport.dataset.scrollMode).toBe("reading");
    expect(rig.writes).toEqual([500, 530]);
  });

  it("waits for the current width and body revision to be observed before correcting follow", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `resize:${index}`, height: 100 })),
      400,
      200
    );
    rig.mount();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(500);
    expect(rig.writes).toEqual([500]);

    rig.setWidth(500);
    for (const { key } of rig.rows) rig.setHeight(key, 120);
    rig.resizeObserver.deliverDelayed();
    expect(rig.writes).toEqual([500]);
    const newWidthEstimate = rig.commitCurrentPlan();
    expect(newWidthEstimate.width).toBe(500);
    expect(newWidthEstimate.rows.every((row) => row.height.kind === "estimate")).toBe(true);
    expect(rig.writes).toEqual([500]);

    const newWidthPlan = rig.flush();
    expect(newWidthPlan.rows.every((row) => row.height.kind === "observed" && row.height.pixels === 120)).toBe(true);
    expect(rig.viewport.scrollTop).toBe(640);
    expect(rig.writes).toEqual([500, 640]);

    rig.setRevision("resize:0", "revision-2");
    rig.setHeight("resize:0", 200);
    const changedBodyEstimate = rig.commitCurrentPlan();
    expect(changedBodyEstimate.rows[0].height).toEqual({ kind: "estimate", pixels: 120, remembered: true });
    expect(rig.geometry.plan(rig.descriptors(), 500).rows[0].height).toEqual({ kind: "observed", pixels: 200 });
    expect(rig.writes).toEqual([500, 640]);

    const changedBodyPlan = rig.flush();
    expect(changedBodyPlan.rows[0].height).toEqual({ kind: "observed", pixels: 200 });
    expect(rig.viewport.scrollTop).toBe(720);
    expect(rig.writes).toEqual([500, 640, 720]);
  });

  it("ignores delayed observer callbacks after disposal", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `disposed:${index}`, height: 80 })),
      400,
      200
    );
    rig.mount();
    rig.flush();
    const writesBeforeDispose = [...rig.writes];
    const requestsBeforeDispose = [...rig.renderRequests];
    const row = rig.rows[0].node;
    const delayedMutation = {
      type: "characterData",
      target: document.createTextNode("late body update"),
    } as unknown as MutationRecord;
    row.append(delayedMutation.target);
    const resizeObserver = rig.resizeObserver;
    const contentObserver = rig.contentObserver;

    rig.dispose();
    resizeObserver.deliverDelayed();
    contentObserver.deliverDelayed([delayedMutation]);

    expect(rig.writes).toEqual(writesBeforeDispose);
    expect(rig.renderRequests).toEqual(requestsBeforeDispose);
  });
});
