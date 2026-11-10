// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HistoryRecorder, downloadHistoryRecording } from "./history_recorder";
import type { HistoryLayout } from "./history_diagnostics";

describe("HistoryRecorder", () => {
  const marks: Array<{ name: string; startTime: number; detail: unknown }> = [];
  const mark = vi.fn((name: string, options: { detail: unknown }) => {
    marks.push({ name, startTime: marks.length + 1, detail: options.detail });
  });
  const clearMarks = vi.fn((name: string) => {
    for (let index = marks.length - 1; index >= 0; index--) {
      if (marks[index].name === name) marks.splice(index, 1);
    }
  });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
    marks.length = 0;
    vi.stubGlobal("performance", {
      now: () => marks.length + 1,
      timeOrigin: 1234,
      mark,
      clearMarks,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    mark.mockClear();
    clearMarks.mockClear();
  });

  const layout = (scopeId: string, viewId: string, scrollTop = 0): HistoryLayout => ({
    scopeId,
    viewId,
    following: true,
    restoring: false,
    capturing: false,
    rowCount: 1,
    virtualTotal: 40,
    viewport: { top: 10, height: 696, width: 1000, scrollTop, scrollHeight: 696 },
    content: { height: 40, scrollHeight: 40 },
    plannedRows: [{ key: "lifecycle:5", kind: "lifecycle_group", entityCount: 2 }],
    virtualRows: [{ index: 0, key: "lifecycle:5", start: 0, size: 40, measured: 40, estimate: 180, remembered: false }],
    mountedRows: [{ index: 0, key: "lifecycle:5", top: -scrollTop, height: 40, width: 1000, busy: false }],
  });

  it("records only after opt-in and keeps simultaneous recorders isolated", () => {
    const first = new HistoryRecorder({ markPrefix: "first:history:" });
    const second = new HistoryRecorder({ markPrefix: "second:history:" });
    first.mark({ kind: "load-older" });
    expect(mark).not.toHaveBeenCalled();

    first.startRecording();
    second.startRecording();
    first.mark({ kind: "marker" });
    second.mark({ kind: "load-older" });
    const firstRecording = first.stopRecording();
    expect(firstRecording?.marks).toEqual([{ name: "first:history:marker", startTime: 1, detail: { kind: "marker" } }]);
    expect(clearMarks).toHaveBeenCalledWith("first:history:marker");
    expect(marks).toContainEqual({
      name: "second:history:load-older",
      startTime: 2,
      detail: { kind: "load-older" },
    });

    second.mark({ kind: "follow", following: false, reason: "wheel-up" });
    const secondRecording = second.stopRecording();
    expect(secondRecording?.marks.map(({ name }) => name)).toEqual([
      "second:history:load-older",
      "second:history:follow",
    ]);
    expect(second.isRecording()).toBe(false);
  });

  it("emits User Timing marks for a browser test when its opt-in callback is enabled", () => {
    let enabled = false;
    const recorder = new HistoryRecorder({ markPrefix: "test:history:", testsEnabled: () => enabled });
    recorder.mark({ kind: "load-older" });
    expect(mark).not.toHaveBeenCalled();
    enabled = true;
    recorder.mark({ kind: "follow", following: false, reason: "wheel-up" });
    expect(mark).toHaveBeenCalledWith("test:history:follow", {
      detail: { kind: "follow", following: false, reason: "wheel-up" },
    });
    expect(recorder.stopRecording()).toBeNull();
  });

  it("captures registered views, changed frame geometry, and scope replacement", () => {
    const recorder = new HistoryRecorder({ markPrefix: "history:" });
    let top = 0;
    const source = vi.fn(() => layout("scope-a", "view-a", top));
    const unregister = recorder.register("view-a", source);
    expect(source).not.toHaveBeenCalled();
    recorder.startRecording();
    vi.advanceTimersToNextFrame();
    top = 248;
    vi.advanceTimersToNextFrame();
    vi.advanceTimersToNextFrame();
    unregister();
    recorder.register("view-b", () => layout("scope-b", "view-b"));
    const recording = recorder.stopRecording();
    expect(
      recording?.layouts.map(({ phase, layout: value }) => [phase, value.scopeId, value.viewport.scrollTop])
    ).toEqual([
      ["start", "scope-a", 0],
      ["frame", "scope-a", 0],
      ["frame", "scope-a", 248],
      ["unmount", "scope-a", 248],
      ["mount", "scope-b", 0],
    ]);
    const calls = source.mock.calls.length;
    vi.advanceTimersToNextFrame();
    expect(source).toHaveBeenCalledTimes(calls);
  });

  it("preserves transient virtual layouts and scroll correction reasons between frames", () => {
    const recorder = new HistoryRecorder({ markPrefix: "history:" });
    recorder.startRecording();
    const inflated = layout("scope-a", "view-a");
    inflated.virtualTotal = 944;
    inflated.virtualRows[0].size = 944;
    inflated.content.height = 944;
    inflated.viewport.scrollHeight = 944;
    recorder.snapshot("virtual-change", () => inflated);
    recorder.snapshot("scroll-before", () => inflated, "resize-follow");
    recorder.snapshot(
      "scroll-after",
      () => ({ ...inflated, viewport: { ...inflated.viewport, scrollTop: 248 } }),
      "resize-follow"
    );
    recorder.snapshot("virtual-change", () => layout("scope-a", "view-a"));
    const recording = recorder.stopRecording();
    expect(
      recording?.layouts.map(({ phase, reason, layout: value }) => [
        phase,
        reason,
        value.virtualTotal,
        value.viewport.scrollTop,
      ])
    ).toEqual([
      ["virtual-change", null, 944, 0],
      ["scroll-before", "resize-follow", 944, 0],
      ["scroll-after", "resize-follow", 944, 248],
      ["virtual-change", null, 40, 0],
    ]);
  });

  it("bounds layout capture and stops reading geometry after the cap", () => {
    const recorder = new HistoryRecorder({ markPrefix: "history:" });
    const source = vi.fn(() => layout("scope-a", "view-a"));
    recorder.startRecording();
    for (let index = 0; index < 1005; index++) recorder.snapshot("commit", source);
    const recording = recorder.stopRecording();
    expect(recording?.layouts).toHaveLength(1000);
    expect(recording?.droppedLayouts).toBe(5);
    expect(source).toHaveBeenCalledTimes(1000);
  });

  it("downloads JSON with the requested prefix and cleans up its temporary link", async () => {
    const recorder = new HistoryRecorder({ markPrefix: "history:" });
    recorder.startRecording();
    recorder.mark({ kind: "marker" });
    const recording = recorder.stopRecording();
    expect(recording).not.toBeNull();
    if (!recording) throw new Error("Missing recording");

    let savedBlob: Blob | undefined;
    const createObjectURL = vi.fn((blob: Blob) => {
      savedBlob = blob;
      return "blob:history-recording";
    });
    const revokeObjectURL = vi.fn();
    const NativeURL = URL;
    vi.stubGlobal(
      "URL",
      class extends NativeURL {
        static createObjectURL = createObjectURL;
        static revokeObjectURL = revokeObjectURL;
      }
    );
    const clickLink = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      expect(this.download).toMatch(/^custom-history-recording-.*\.json$/);
      expect(this.href).toBe("blob:history-recording");
    });
    try {
      downloadHistoryRecording(recording, "custom-history-recording");
      expect(clickLink).toHaveBeenCalledOnce();
      expect(createObjectURL).toHaveBeenCalledOnce();
      expect(savedBlob?.type).toBe("application/json");
      expect(savedBlob).toBeDefined();
      if (!savedBlob) throw new Error("Missing download blob");
      expect(JSON.parse(await savedBlob.text())).toMatchObject({
        version: 4,
        marks: [{ name: "history:marker", detail: { kind: "marker" } }],
      });
      expect(document.querySelector('a[href="blob:history-recording"]')).toBeNull();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:history-recording");
    } finally {
      clickLink.mockRestore();
    }
  });

  it("drops excess marks instead of growing the native timeline indefinitely", () => {
    const recorder = new HistoryRecorder({ markPrefix: "history:" });
    recorder.startRecording();
    for (let index = 0; index < 20_005; index++) recorder.mark({ kind: "load-older" });
    const recording = recorder.stopRecording();
    expect(recording?.dropped).toBe(5);
    expect(recording?.marks).toHaveLength(20_000);
    expect(mark).toHaveBeenCalledTimes(20_000);
  });
});
