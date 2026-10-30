// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  markThreadViewEvent,
  downloadThreadViewRecording,
  ThreadViewRecorder,
  type ThreadViewLayout,
} from "./thread_view_recorder";

describe("ThreadViewRecorder User Timing", () => {
  const marks: Array<{ name: string; startTime: number; detail: unknown }> = [];
  const mark = vi.fn((name: string, options: { detail: unknown }) => {
    marks.push({ name, startTime: marks.length + 1, detail: options.detail });
  });
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
    marks.length = 0;
    vi.stubGlobal("performance", {
      now: () => 0,
      timeOrigin: 1234,
      mark,
      clearMarks: vi.fn(),
      getEntriesByType: () => marks,
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    mark.mockClear();
  });

  it("does not emit marks until the reader opts in", () => {
    const trace = new ThreadViewRecorder();
    markThreadViewEvent({ kind: "load-older" });
    expect(mark).not.toHaveBeenCalled();
    trace.startRecording();
    markThreadViewEvent({ kind: "input", source: "wheel", direction: "up", scrollTop: 200 });
    markThreadViewEvent({ kind: "virtual-size", before: 2000, after: 1000, sync: false, following: false });
    const capture = trace.stopRecording();
    expect(capture?.marks).toEqual([
      {
        name: "agentplane:thread-view:input",
        startTime: 1,
        detail: { kind: "input", source: "wheel", direction: "up", scrollTop: 200 },
      },
      {
        name: "agentplane:thread-view:virtual-size",
        startTime: 2,
        detail: { kind: "virtual-size", before: 2000, after: 1000, sync: false, following: false },
      },
    ]);
    expect(capture?.version).toBe(3);
    expect(capture?.timeOrigin).toBe(1234);
    markThreadViewEvent({ kind: "load-older" });
    expect(mark).toHaveBeenCalledTimes(2);
  });

  const layout = (threadId: string, viewId: string, scrollTop = 0): ThreadViewLayout => ({
    threadId,
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

  it("captures the existing view, changed frame geometry, and thread replacement", () => {
    const trace = new ThreadViewRecorder();
    let top = 0;
    const source = vi.fn(() => layout("thread-a", "view-a", top));
    const unregister = trace.register("view-a", source);
    expect(source).not.toHaveBeenCalled();
    trace.startRecording();
    vi.advanceTimersToNextFrame();
    top = 248;
    vi.advanceTimersToNextFrame();
    vi.advanceTimersToNextFrame();
    unregister();
    trace.register("view-b", () => layout("thread-b", "view-b"));
    const capture = trace.stopRecording();
    expect(
      capture?.layouts.map((entry) => [entry.phase, entry.layout.threadId, entry.layout.viewport.scrollTop])
    ).toEqual([
      ["start", "thread-a", 0],
      ["frame", "thread-a", 0],
      ["frame", "thread-a", 248],
      ["unmount", "thread-a", 248],
      ["mount", "thread-b", 0],
    ]);
    const calls = source.mock.calls.length;
    vi.advanceTimersToNextFrame();
    expect(source).toHaveBeenCalledTimes(calls);
  });

  it("preserves transient virtual layouts and scroll correction reasons between frames", () => {
    const trace = new ThreadViewRecorder();
    trace.startRecording();
    const inflated = layout("thread-a", "view-a");
    inflated.virtualTotal = 944;
    inflated.virtualRows[0].size = 944;
    inflated.content.height = 944;
    inflated.viewport.scrollHeight = 944;
    trace.snapshot("virtual-change", () => inflated);
    trace.snapshot("scroll-before", () => inflated, "resize-follow");
    trace.snapshot(
      "scroll-after",
      () => ({ ...inflated, viewport: { ...inflated.viewport, scrollTop: 248 } }),
      "resize-follow"
    );
    trace.snapshot("virtual-change", () => layout("thread-a", "view-a"));
    const capture = trace.stopRecording();
    expect(
      capture?.layouts.map((entry) => [
        entry.phase,
        entry.reason,
        entry.layout.virtualTotal,
        entry.layout.viewport.scrollTop,
      ])
    ).toEqual([
      ["virtual-change", null, 944, 0],
      ["scroll-before", "resize-follow", 944, 0],
      ["scroll-after", "resize-follow", 944, 248],
      ["virtual-change", null, 40, 0],
    ]);
  });

  it("bounds layout capture and stops reading geometry after the cap", () => {
    const trace = new ThreadViewRecorder();
    const source = vi.fn(() => layout("thread-a", "view-a"));
    trace.startRecording();
    for (let index = 0; index < 1005; index++) trace.snapshot("commit", source);
    const capture = trace.stopRecording();
    expect(capture?.layouts).toHaveLength(1000);
    expect(capture?.droppedLayouts).toBe(5);
    expect(source).toHaveBeenCalledTimes(1000);
  });

  it("downloads the recorded marks as local JSON and cleans up its temporary link", async () => {
    const trace = new ThreadViewRecorder();
    trace.startRecording();
    markThreadViewEvent({ kind: "marker" });
    const recording = trace.stopRecording();
    expect(recording).not.toBeNull();
    if (!recording) throw new Error("Missing recording");

    let savedBlob: Blob | undefined;
    const createObjectURL = vi.fn((blob: Blob) => {
      savedBlob = blob;
      return "blob:scroll-recording";
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
      expect(this.download).toMatch(/^agentplane-thread-view-recording-.*\.json$/);
      expect(this.href).toBe("blob:scroll-recording");
    });
    try {
      downloadThreadViewRecording(recording);
      expect(clickLink).toHaveBeenCalledOnce();
      expect(createObjectURL).toHaveBeenCalledOnce();
      expect(savedBlob?.type).toBe("application/json");
      expect(savedBlob).toBeDefined();
      if (!savedBlob) throw new Error("Missing download blob");
      expect(JSON.parse(await savedBlob.text())).toMatchObject({
        version: 3,
        marks: [{ name: "agentplane:thread-view:marker", detail: { kind: "marker" } }],
      });
      expect(document.querySelector('a[href="blob:scroll-recording"]')).toBeNull();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:scroll-recording");
    } finally {
      clickLink.mockRestore();
    }
  });

  it("drops excess marks rather than growing the native timeline indefinitely", () => {
    const trace = new ThreadViewRecorder();
    trace.startRecording();
    for (let i = 0; i < 20_005; i++) markThreadViewEvent({ kind: "load-older" });
    expect(trace.stopRecording()?.dropped).toBe(5);
    expect(mark).toHaveBeenCalledTimes(20_000);
  });
});

it("emits User Timing marks only when the test harness enables recording", () => {
  const mark = vi.fn();
  vi.stubGlobal("performance", { mark });
  try {
    markThreadViewEvent({ kind: "load-older" });
    expect(mark).not.toHaveBeenCalled();
    window.__agentplaneThreadViewMarksEnabled = true;
    markThreadViewEvent({ kind: "follow", following: false, reason: "wheel-up" });
    expect(mark).toHaveBeenCalledWith("agentplane:thread-view:follow", {
      detail: { kind: "follow", following: false, reason: "wheel-up" },
    });
  } finally {
    delete window.__agentplaneThreadViewMarksEnabled;
    vi.unstubAllGlobals();
  }
});
