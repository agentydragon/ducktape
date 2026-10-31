import { describe, expect, it } from "vitest";

import { HistoryGeometry, mountedRange, type RowDescriptor } from "./history_geometry";

const descriptor = (key: string, revision = "revision-1"): RowDescriptor => ({ key, revision });

describe("HistoryGeometry", () => {
  it("promotes a measurement equal to its estimate and reports only evidence changes", () => {
    const geometry = new HistoryGeometry("geometry-equal-estimate");
    const plan = geometry.plan([descriptor("row")], 400);
    const committed = geometry.commit(plan);

    expect(plan.rows[0].height).toEqual({ kind: "estimate", pixels: 180, remembered: false });
    expect(geometry.observe(committed, [{ key: "row", width: 400, height: 180 }])).toBe(true);
    expect(geometry.observe(committed, [{ key: "row", width: 400, height: 180 }])).toBe(false);
    expect(geometry.plan([descriptor("row")], 400).rows[0].height).toEqual({ kind: "observed", pixels: 180 });
  });

  it("uses width-specific hints and prevents a changed revision from reusing its old height", () => {
    const geometry = new HistoryGeometry("geometry-width-revision");
    const initial = geometry.commit(geometry.plan([descriptor("row")], 400));
    expect(geometry.observe(initial, [{ key: "row", width: 400, height: 112 }])).toBe(true);

    const otherWidthPlan = geometry.plan([descriptor("row")], 401);
    expect(otherWidthPlan.rows[0].height).toEqual({ kind: "estimate", pixels: 180, remembered: false });
    const otherWidth = geometry.commit(otherWidthPlan);
    expect(geometry.observe(otherWidth, [{ key: "row", width: 401, height: 136 }])).toBe(true);
    expect(geometry.observe(initial, [{ key: "row", width: 400, height: 112 }])).toBe(false);

    const returnedWidth = geometry.plan([descriptor("row")], 400);
    expect(returnedWidth.rows[0].height).toEqual({ kind: "estimate", pixels: 112, remembered: true });
    const restoredWidth = geometry.commit(returnedWidth);
    expect(geometry.observe(restoredWidth, [{ key: "row", width: 400, height: 112 }])).toBe(true);

    const changedRevisionPlan = geometry.plan([descriptor("row", "revision-2")], 400);
    expect(changedRevisionPlan.rows[0].height).toEqual({ kind: "estimate", pixels: 112, remembered: true });
    const changedRevision = geometry.commit(changedRevisionPlan);
    expect(geometry.observe(otherWidth, [{ key: "row", width: 401, height: 112 }])).toBe(false);
    expect(geometry.plan([descriptor("row", "revision-2")], 400).rows[0].height).toEqual({
      kind: "estimate",
      pixels: 112,
      remembered: true,
    });
    expect(geometry.observe(changedRevision, [{ key: "row", width: 400, height: 91 }])).toBe(true);
    expect(geometry.plan([descriptor("row", "revision-2")], 400).rows[0].height).toEqual({
      kind: "observed",
      pixels: 91,
    });
  });

  it("rejects stale and cross-geometry commits without admitting their measurements", () => {
    const first = new HistoryGeometry("geometry-owner-first");
    const second = new HistoryGeometry("geometry-owner-second");
    const stale = first.commit(first.plan([descriptor("row")], 420));
    const current = first.commit(first.plan([descriptor("row", "revision-2")], 420));

    expect(first.observe(stale, [{ key: "row", width: 420, height: 75 }])).toBe(false);
    expect(() => second.observe(current, [{ key: "row", width: 420, height: 75 }])).toThrow(TypeError);
    expect(() => second.commit(current.plan)).toThrow(TypeError);
    expect(second.plan([descriptor("row", "revision-2")], 420).rows[0].height).toEqual({
      kind: "estimate",
      pixels: 180,
      remembered: false,
    });
  });

  it("preserves row evidence across reorder and prepend while recomputing prefix offsets", () => {
    const geometry = new HistoryGeometry("geometry-reorder-prepend");
    const original = [descriptor("a"), descriptor("b")];
    const committed = geometry.commit(geometry.plan(original, 500));
    expect(
      geometry.observe(committed, [
        { key: "a", width: 500, height: 55 },
        { key: "b", width: 500, height: 73 },
      ])
    ).toBe(true);

    const prependedAndReordered = geometry.plan([descriptor("new"), descriptor("b"), descriptor("a")], 500);
    expect(prependedAndReordered.rows.map(({ key, start, height }) => [key, start, height.pixels])).toEqual([
      ["new", 0, 180],
      ["b", 180, 73],
      ["a", 253, 55],
    ]);
    expect(prependedAndReordered.total).toBe(308);
  });

  it("keeps invalidated evidence as an estimate until the new generation is observed", () => {
    const geometry = new HistoryGeometry("geometry-generation");
    const committed = geometry.commit(geometry.plan([descriptor("row")], 440));
    expect(geometry.observe(committed, [{ key: "row", width: 440, height: 101 }])).toBe(true);

    geometry.invalidate(["row"]);
    expect(geometry.observe(committed, [{ key: "row", width: 440, height: 103 }])).toBe(false);
    const invalidated = geometry.plan([descriptor("row")], 440);
    expect(invalidated.rows[0]).toMatchObject({
      generation: 1,
      height: { kind: "estimate", pixels: 101, remembered: true },
    });

    const nextCommit = geometry.commit(invalidated);
    expect(geometry.observe(nextCommit, [{ key: "row", width: 440, height: 101 }])).toBe(true);
    expect(geometry.observe(nextCommit, [{ key: "row", width: 440, height: 101 }])).toBe(false);
    expect(geometry.plan([descriptor("row")], 440).rows[0].height).toEqual({ kind: "observed", pixels: 101 });
  });

  it("keeps every prefix and total consistent through a seeded prepend and reorder", () => {
    let state = 0x5eed1234;
    const nextRandom = (): number => {
      state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0;
      return state;
    };
    const inputRows = Array.from({ length: 48 }, (_, index) => descriptor(`seeded:${index}`));
    const inputHeights = new Map<string, number>();
    for (const { key } of inputRows) inputHeights.set(key, 24 + (nextRandom() % 277));
    const geometry = new HistoryGeometry("geometry-seeded-arithmetic");
    const measured = geometry.commit(geometry.plan(inputRows, 760));
    expect(
      geometry.observe(
        measured,
        inputRows.map(({ key }) => ({ key, width: 760, height: inputHeights.get(key) ?? 0 }))
      )
    ).toBe(true);

    const reordered = [...inputRows];
    for (let index = reordered.length - 1; index > 0; index--) {
      const swapIndex = nextRandom() % (index + 1);
      [reordered[index], reordered[swapIndex]] = [reordered[swapIndex], reordered[index]];
    }
    const arrangement = [descriptor("seeded:prepended-a"), descriptor("seeded:prepended-b"), ...reordered];
    const plan = geometry.plan(arrangement, 760);
    let expectedStart = 0;
    for (const [index, row] of plan.rows.entries()) {
      const expectedHeight = inputHeights.get(arrangement[index].key) ?? 180;
      expect(row.key).toBe(arrangement[index].key);
      expect(row.start).toBe(expectedStart);
      expect(row.height.pixels).toBe(expectedHeight);
      expectedStart += expectedHeight;
    }
    expect(plan.total).toBe(expectedStart);
  });
});

describe("mountedRange", () => {
  it("returns intersecting rows in order and retains requested anchors", () => {
    const rows = Array.from({ length: 10 }, (_, index) => descriptor(`range:${index}`));
    const plan = new HistoryGeometry("geometry-mounted-range").plan(rows, 600);

    expect(mountedRange(plan, 360, 180, 90, new Set(["range:8", "missing"])).map((row) => row.key)).toEqual([
      "range:1",
      "range:2",
      "range:3",
      "range:8",
    ]);
    expect(mountedRange(plan, 181, 180, 0, new Set()).map((row) => row.key)).toEqual(["range:1", "range:2"]);
  });

  it("returns retained anchors when the viewport falls beyond the planned extent", () => {
    const plan = new HistoryGeometry("geometry-mounted-range-retained").plan(
      [descriptor("range:first"), descriptor("range:second")],
      600
    );

    expect(mountedRange(plan, 10_000, 100, 0, new Set(["range:second"])).map((row) => row.key)).toEqual([
      "range:second",
    ]);
  });
});
