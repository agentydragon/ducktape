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
    viewportHeight: number,
    loadOlder: () => void = () => undefined
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
      loadOlder,
      undefined
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
    for (const { key, node } of this.rows) {
      entities.set(key, key);
      for (const anchor of node.querySelectorAll<HTMLElement>("[data-history-anchor-key]")) {
        const anchorKey = anchor.dataset.historyAnchorKey;
        if (anchorKey) entities.set(anchorKey, key);
      }
    }
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

  it("keeps the committed reader anchor across growth, another wheel input, and scrollend", () => {
    const rig = new ViewportRig(
      Array.from({ length: 10 }, (_, index) => ({ key: `wheel-resize:${index}`, height: 100 })),
      400,
      200
    );
    const growingRow = rig.rows[1].node;
    const bodyText = document.createTextNode("before expansion");
    growingRow.append(bodyText);

    rig.mount();
    rig.flush();
    rig.controller.pointerDown();
    rig.userScrollTo(400);
    rig.controller.scrolled();
    rig.controller.pointerUp();
    rig.viewport.dispatchEvent(new Event("scrollend"));
    rig.flush();

    const anchorRow = rig.rows[4].node;
    const originalAnchorTop = anchorRow.getBoundingClientRect().top - rig.viewport.getBoundingClientRect().top;
    expect(originalAnchorTop).toBe(0);
    expect(rig.viewport.scrollTop).toBe(400);

    bodyText.data = "expanded body above the reader anchor";
    rig.setHeight("wheel-resize:1", 160);
    // Model the browser's downstream reflow before React commits the corrected plan.
    anchorRow.style.top = "460px";
    rig.resizeObserver.deliverDelayed();
    expect(rig.renderRequests.length).toBeGreaterThan(0);

    rig.controller.input("wheel", "up", true);
    rig.viewport.dispatchEvent(new Event("scrollend"));
    rig.userScrollTo(360);
    rig.controller.scrolled();
    const updatedPlan = rig.flush();

    expect(updatedPlan.total).toBe(1060);
    expect(rig.viewport.scrollTop).toBe(420);
    expect(anchorRow.getBoundingClientRect().top - rig.viewport.getBoundingClientRect().top).toBe(
      originalAnchorTop + 40
    );
    expect(rig.writes).toEqual([800, 420]);
  });

  it("keeps a nested reader anchor steady when older records prepend within the same row", () => {
    const rig = new ViewportRig(
      [
        { key: "group:stable", height: 500 },
        { key: "group:following-a", height: 100 },
        { key: "group:following-b", height: 100 },
      ],
      400,
      200
    );
    const group = rig.rows[0];
    const step = document.createElement("div");
    step.dataset.historyAnchorKey = "step:group";
    group.node.append(step);
    let stepHeight = 300;
    Object.defineProperties(step, {
      getBoundingClientRect: {
        configurable: true,
        value: () =>
          rect((Number.parseFloat(group.node.style.top) || 0) + 200 - rig.viewport.scrollTop, 400, stepHeight),
      },
      getClientRects: {
        configurable: true,
        value: () => [step.getBoundingClientRect()],
      },
    });
    let readerRecordInset = 250;
    const makeReaderRecord = (): HTMLDivElement => {
      const record = document.createElement("div");
      record.dataset.historyAnchorKey = "reasoning:reader";
      Object.defineProperties(record, {
        getBoundingClientRect: {
          configurable: true,
          value: () =>
            rect((Number.parseFloat(group.node.style.top) || 0) + readerRecordInset - rig.viewport.scrollTop, 400, 20),
        },
        getClientRects: {
          configurable: true,
          value: () => [record.getBoundingClientRect()],
        },
      });
      return record;
    };
    let readerRecord = makeReaderRecord();
    step.append(readerRecord);

    rig.mount();
    rig.flush();
    rig.controller.pointerDown();
    rig.userScrollTo(250);
    rig.controller.scrolled();
    rig.controller.pointerUp();
    rig.flush();

    const viewportTop = rig.viewport.getBoundingClientRect().top;
    const originalReaderOffset = readerRecord.getBoundingClientRect().top - viewportTop;
    expect(originalReaderOffset).toBe(0);
    expect(rig.viewport.dataset.scrollMode).toBe("reading");

    const olderRecord = document.createElement("div");
    olderRecord.dataset.historyAnchorKey = "reasoning:older";
    Object.defineProperties(olderRecord, {
      getBoundingClientRect: {
        configurable: true,
        value: () => rect((Number.parseFloat(group.node.style.top) || 0) + 200 - rig.viewport.scrollTop, 400, 80),
      },
      getClientRects: {
        configurable: true,
        value: () => [olderRecord.getBoundingClientRect()],
      },
    });
    step.insertBefore(olderRecord, readerRecord);
    group.height += 80;
    group.revision = "revision-2";
    stepHeight += 80;
    readerRecordInset += 80;
    const removedReaderRecord = readerRecord;
    const replacementReaderRecord = makeReaderRecord();
    readerRecord.replaceWith(replacementReaderRecord);
    readerRecord = replacementReaderRecord;

    const prepend = {
      type: "childList",
      target: step,
      addedNodes: [olderRecord],
      removedNodes: [],
    } as unknown as MutationRecord;
    const remount = {
      type: "childList",
      target: step,
      addedNodes: [replacementReaderRecord],
      removedNodes: [removedReaderRecord],
    } as unknown as MutationRecord;
    rig.contentObserver.deliverDelayed([prepend, remount]);
    const updatedPlan = rig.flush();

    expect(updatedPlan.rows[0].height).toEqual({ kind: "observed", pixels: 580 });
    expect(rig.viewport.scrollTop).toBe(330);
    expect(readerRecord.getBoundingClientRect().top - viewportTop).toBe(originalReaderOffset);
  });

  it("releases nested anchor targets when the reader scrolls through an expanded group", () => {
    const rig = new ViewportRig(
      [
        { key: "intro", height: 100 },
        { key: "expanded-group", height: 500 },
        { key: "tail", height: 100 },
      ],
      400,
      200
    );
    const group = rig.rows[1].node;
    const control = document.createElement("button");
    const step = document.createElement("div");
    step.dataset.historyAnchorKey = "step:reader";
    group.append(control, step);
    for (const [node, inset, height] of [
      [control, 0, 20],
      [step, 300, 200],
    ] as const) {
      Object.defineProperties(node, {
        getBoundingClientRect: {
          configurable: true,
          value: () => rect(group.getBoundingClientRect().top + inset, 400, height),
        },
        getClientRects: { configurable: true, value: () => [node.getBoundingClientRect()] },
      });
    }
    rig.mount();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(500);

    rig.controller.disclose(control);
    rig.setHeight("expanded-group", 700);
    rig.resizeObserver.deliverDelayed();
    rig.flush();
    const writesBeforeScrolling = rig.writes.length;
    for (const top of [480, 460, 440]) {
      rig.controller.input("wheel", "up", true);
      rig.userScrollTo(top);
      rig.controller.scrolled();
      rig.flush();
      expect(rig.viewport.scrollTop).toBe(top);
      rig.viewport.dispatchEvent(new Event("scrollend"));
      rig.flush();
      expect(rig.viewport.scrollTop).toBe(top);
    }
    expect(rig.writes).toHaveLength(writesBeforeScrolling);
  });

  it("preserves following through an intermediate browser clamp after content shrinks", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `clamp:${index}`, height: 100 })),
      400,
      200
    );
    rig.mount();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(500);
    expect(rig.writes).toEqual([500]);

    for (const { key } of rig.rows) rig.setHeight(key, 80);
    const finalMaximum = 7 * 80 - 200;
    rig.userScrollTo(finalMaximum - 40);
    rig.resizeObserver.deliverDelayed();
    expect(rig.viewport.scrollTop).toBe(finalMaximum - 40);
    expect(rig.writes).toEqual([500]);

    const updatedPlan = rig.flush();
    expect(updatedPlan.total).toBe(7 * 80);
    expect(rig.viewport.dataset.scrollMode).toBe("following");
    expect(rig.viewport.scrollTop).toBe(finalMaximum);
    expect(rig.writes).toEqual([500, finalMaximum]);
  });

  it("loads older rows from a verified near-top layout during an active gesture", () => {
    let loadOlderCalls = 0;
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `older:${index}`, height: 100 })),
      400,
      200,
      () => loadOlderCalls++
    );
    rig.mount();
    rig.flush();
    expect(loadOlderCalls).toBe(0);

    rig.controller.pointerDown();
    rig.userScrollTo(150);
    rig.controller.scrolled();
    rig.flush();

    expect(rig.controller.snapshot()?.capturing).toBe(true);
    expect(rig.viewport.scrollTop).toBe(150);
    expect(loadOlderCalls).toBe(1);
  });

  it("keeps a deep disclosure control in range, then follows the row after native scrolling", () => {
    const rig = new ViewportRig(
      Array.from({ length: 14 }, (_, index) => ({ key: `nested:${index}`, height: index === 0 ? 700 : 100 })),
      400,
      200
    );
    rig.mount();
    const plan = rig.flush();

    rig.controller.pointerDown();
    rig.userScrollTo(550);
    rig.controller.scrolled();
    rig.controller.pointerUp();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(550);

    const row = rig.rows[0].node;
    const control = document.createElement("button");
    row.append(control);
    let controlInset = 600;
    Object.defineProperty(control, "getBoundingClientRect", {
      configurable: true,
      value: () => {
        const rowBox = row.getBoundingClientRect();
        return rect(rowBox.top + controlInset, rig.controller.width, 20);
      },
    });
    Object.defineProperty(control, "getClientRects", {
      configurable: true,
      value: () => [control.getBoundingClientRect()],
    });

    rig.controller.disclose(control);
    const entities = new Map(rig.rows.map(({ key }) => [key, key]));
    const mountedKeys = rig.controller.range(plan, entities).map(({ key }) => key);
    expect(mountedKeys).toContain("nested:1");
    expect(mountedKeys).toContain("nested:3");

    const writesBeforeNativeScroll = rig.writes.length;
    rig.userScrollTo(580);
    rig.controller.scrolled();
    controlInset = 640;
    rig.flush();

    expect(rig.viewport.scrollTop).toBe(580);
    expect(rig.writes).toHaveLength(writesBeforeNativeScroll);
  });

  it("preserves the row position when its deeply clicked disclosure target disappears", () => {
    const rig = new ViewportRig(
      Array.from({ length: 14 }, (_, index) => ({ key: `removed:${index}`, height: index === 0 ? 700 : 100 })),
      400,
      200
    );
    rig.mount();
    rig.flush();

    rig.controller.pointerDown();
    rig.userScrollTo(550);
    rig.controller.scrolled();
    rig.controller.pointerUp();
    rig.flush();

    const row = rig.rows[0].node;
    const control = document.createElement("button");
    row.append(control);
    Object.defineProperty(control, "getBoundingClientRect", {
      configurable: true,
      value: () => {
        const rowBox = row.getBoundingClientRect();
        return rect(rowBox.top + 600, rig.controller.width, 20);
      },
    });
    Object.defineProperty(control, "getClientRects", {
      configurable: true,
      value: () => [control.getBoundingClientRect()],
    });
    rig.controller.disclose(control);
    const originalRowOffset = row.getBoundingClientRect().top - rig.viewport.getBoundingClientRect().top;
    const writesBeforeExpansion = rig.writes.length;

    control.remove();
    rig.setHeight("removed:0", 730);
    const removal = {
      type: "childList",
      target: row,
      removedNodes: [control],
      addedNodes: [],
    } as unknown as MutationRecord;
    expect(() => rig.contentObserver.deliverDelayed([removal])).not.toThrow();
    expect(rig.writes).toHaveLength(writesBeforeExpansion);

    const expandedPlan = rig.flush();
    expect(expandedPlan.rows[0].height).toEqual({ kind: "observed", pixels: 730 });
    expect(rig.viewport.scrollTop).toBe(550);
    expect(row.getBoundingClientRect().top - rig.viewport.getBoundingClientRect().top).toBe(originalRowOffset);
    expect(rig.writes).toHaveLength(writesBeforeExpansion);
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

  it("requests a fresh plan for descendant content mutations before observing changed row geometry", () => {
    const rig = new ViewportRig(
      Array.from({ length: 7 }, (_, index) => ({ key: `mutation:${index}`, height: 100 })),
      400,
      200
    );
    const row = rig.rows[6].node;
    const bodyText = document.createTextNode("initial body");
    row.append(bodyText);

    rig.mount();
    rig.flush();
    expect(rig.viewport.scrollTop).toBe(500);
    const writesBeforeMutation = [...rig.writes];

    bodyText.data = "body expanded after the current layout was committed";
    rig.setHeight("mutation:6", 130);
    const record = { type: "characterData", target: bodyText } as unknown as MutationRecord;
    expect(() => rig.contentObserver.deliverDelayed([record])).not.toThrow();

    expect(rig.renderRequests).toHaveLength(1);
    expect(rig.writes).toEqual(writesBeforeMutation);
    const updatedPlan = rig.flush();
    expect(updatedPlan.rows[6].height).toEqual({ kind: "observed", pixels: 130 });
    expect(updatedPlan.total).toBe(730);
    expect(rig.viewport.scrollTop).toBe(530);
    expect(rig.writes).toEqual([500, 530]);
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
