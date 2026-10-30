import { startThreadViewMarks, stopThreadViewMarks, type ThreadViewEvent } from "./thread_view_timing";

const MARK_PREFIX = "agentplane:thread-view:";
const MAX_LAYOUT_SNAPSHOTS = 1_000;
const MAX_BROWSER_ENTRIES = 5_000;
const BROWSER_TYPES = ["layout-shift", "longtask", "long-animation-frame"] as const;

/** Geometry and identifiers only; never message text, arguments, or output. */
export interface ScrollLayout {
  threadId: string;
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

type SnapshotPhase =
  "start" | "mount" | "unmount" | "commit" | "frame" | "virtual-change" | "scroll-before" | "scroll-after";
type LayoutSource = () => ScrollLayout | null;
interface LayoutSnapshot {
  at: number;
  phase: SnapshotPhase;
  reason: string | null;
  layout: ScrollLayout;
}

interface BrowserEntry {
  entryType: string;
  startTime: number;
  duration: number;
  value?: number;
  hadRecentInput?: boolean;
}

/** JSON for inspection, not a Chrome DevTools profile. No DOM text or resource URLs. */
export interface ScrollRecording {
  version: 3;
  startedAt: string;
  savedAt: string;
  timeOrigin: number;
  viewport: { width: number; height: number; devicePixelRatio: number };
  dropped: number;
  marks: { name: string; startTime: number; detail: ThreadViewEvent }[];
  browser: BrowserEntry[];
  layouts: LayoutSnapshot[];
  droppedLayouts: number;
}

interface ActiveRecording {
  startedAt: string;
  startTime: number;
  dropped: number;
  browser: BrowserEntry[];
  observers: PerformanceObserver[];
  layouts: LayoutSnapshot[];
  droppedLayouts: number;
  previousFrames: Map<string, string>;
}

/** The browser owns the mark timeline; this class collects only opt-in browser metrics. */
export class ScrollCapture {
  #recording: ActiveRecording | null = null;
  #sources = new Map<string, LayoutSource>();
  #frame: number | null = null;

  register(viewId: string, source: LayoutSource): () => void {
    this.#sources.set(viewId, source);
    this.snapshot("mount", source);
    return () => {
      this.snapshot("unmount", source);
      this.#sources.delete(viewId);
      this.#recording?.previousFrames.delete(viewId);
    };
  }

  snapshotView(phase: SnapshotPhase, viewId: string, reason: string): void {
    const source = this.#sources.get(viewId);
    if (source) this.snapshot(phase, source, reason);
  }

  snapshot(phase: SnapshotPhase, source: LayoutSource, reason: string | null = null): void {
    const recording = this.#recording;
    if (!recording) return;
    if (recording.layouts.length >= MAX_LAYOUT_SNAPSHOTS) {
      recording.droppedLayouts++;
      return;
    }
    const layout = source();
    if (!layout) return;
    if (phase === "frame") {
      const serialized = JSON.stringify(layout);
      if (recording.previousFrames.get(layout.viewId) === serialized) return;
      recording.previousFrames.set(layout.viewId, serialized);
    }
    recording.layouts.push({ at: performance.now(), phase, reason, layout });
  }

  #sampleFrames(): void {
    this.#frame = requestAnimationFrame(() => {
      this.#frame = null;
      const recording = this.#recording;
      if (!recording) return;
      for (const source of this.#sources.values()) this.snapshot("frame", source);
      if (recording.layouts.length < MAX_LAYOUT_SNAPSHOTS) this.#sampleFrames();
    });
  }

  startRecording(): void {
    if (this.#recording) return;
    const recording = {
      startedAt: new Date().toISOString(),
      startTime: performance.now(),
      dropped: 0,
      browser: [] as BrowserEntry[],
      observers: [] as PerformanceObserver[],
      layouts: [] as LayoutSnapshot[],
      droppedLayouts: 0,
      previousFrames: new Map<string, string>(),
    } satisfies ActiveRecording;
    this.#recording = recording;
    startThreadViewMarks();
    for (const source of this.#sources.values()) this.snapshot("start", source);
    this.#sampleFrames();
    // These entry types are not available via performance.getEntriesByType(). Observe them live.
    if (typeof PerformanceObserver === "undefined") return;
    for (const type of BROWSER_TYPES) {
      if (!PerformanceObserver.supportedEntryTypes?.includes(type)) continue;
      const observer = new PerformanceObserver((list) =>
        list.getEntries().forEach((entry) => this.#addBrowserEntry(recording, entry))
      );
      observer.observe({ type });
      recording.observers.push(observer);
    }
  }

  #addBrowserEntry(recording: ActiveRecording, entry: PerformanceEntry): void {
    if (recording.browser.length >= MAX_BROWSER_ENTRIES) {
      recording.dropped++;
      return;
    }
    const shift = entry as PerformanceEntry & { value?: number; hadRecentInput?: boolean };
    recording.browser.push({
      entryType: entry.entryType,
      startTime: entry.startTime,
      duration: entry.duration,
      ...(entry.entryType === "layout-shift" ? { value: shift.value, hadRecentInput: shift.hadRecentInput } : {}),
    });
  }

  isRecording(): boolean {
    return this.#recording !== null;
  }

  stopRecording(): ScrollRecording | null {
    const recording = this.#recording;
    if (!recording) return null;
    // Collect entries delivered after the last observer callback before disconnecting.
    for (const observer of recording.observers) {
      // Delivery of takeRecords is not automatic; collect the final entries explicitly.
      for (const entry of observer.takeRecords()) this.#addBrowserEntry(recording, entry);
      observer.disconnect();
    }
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#frame = null;
    this.#recording = null;
    const droppedMarks = stopThreadViewMarks();
    const marks = performance
      .getEntriesByType("mark")
      .filter((entry) => entry.name.startsWith(MARK_PREFIX) && entry.startTime >= recording.startTime)
      .map((entry) => ({
        name: entry.name,
        startTime: entry.startTime,
        detail: (entry as PerformanceMark).detail as ThreadViewEvent,
      }));
    if (!window.__agentplaneThreadViewMarksEnabled) {
      for (const kind of new Set(marks.map(({ name }) => name))) performance.clearMarks(kind);
    }
    return {
      version: 3,
      startedAt: recording.startedAt,
      savedAt: new Date().toISOString(),
      timeOrigin: performance.timeOrigin,
      viewport: { width: window.innerWidth, height: window.innerHeight, devicePixelRatio: window.devicePixelRatio },
      dropped: recording.dropped + droppedMarks,
      marks,
      browser: recording.browser,
      layouts: recording.layouts,
      droppedLayouts: recording.droppedLayouts,
    };
  }
}

/** One capture across the page: it survives switching between threads. */
export const scrollCapture: ScrollCapture = new ScrollCapture();

/** On-device diagnostics only: nothing is uploaded. Row keys can contain identifying metadata. */
export function downloadScrollDiagnostics(recording: ScrollRecording): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(recording, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `agentplane-scroll-diagnostics-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
