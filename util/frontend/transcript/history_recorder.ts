import type { HistoryDiagnostics, HistoryEvent, HistoryLayout, SnapshotPhase } from "./history_diagnostics";

const MAX_CAPTURE_MARKS = 20_000;
const MAX_LAYOUT_SNAPSHOTS = 1_000;
const MAX_BROWSER_ENTRIES = 5_000;
const BROWSER_TYPES = ["layout-shift", "longtask", "long-animation-frame"] as const;

export interface HistoryRecorderOptions {
  markPrefix: string;
  testsEnabled?: () => boolean;
}

export interface HistoryLayoutSnapshot {
  at: number;
  phase: SnapshotPhase;
  reason: string | null;
  layout: HistoryLayout;
}

interface BrowserEntry {
  entryType: string;
  startTime: number;
  duration: number;
  value?: number;
  hadRecentInput?: boolean;
}

/** JSON for inspection, not a Chrome DevTools profile. No DOM text or resource URLs. */
export interface HistoryRecording {
  version: 4;
  startedAt: string;
  savedAt: string;
  timeOrigin: number;
  viewport: { width: number; height: number; devicePixelRatio: number };
  dropped: number;
  marks: { name: string; startTime: number; detail: HistoryEvent }[];
  browser: BrowserEntry[];
  layouts: HistoryLayoutSnapshot[];
  droppedLayouts: number;
}

interface ActiveRecording {
  startedAt: string;
  droppedMarks: number;
  marks: HistoryRecording["marks"];
  droppedBrowser: number;
  browser: BrowserEntry[];
  observers: PerformanceObserver[];
  layouts: HistoryLayoutSnapshot[];
  droppedLayouts: number;
  previousFrames: Map<string, string>;
}

type LayoutSource = () => HistoryLayout | null;

/** Opt-in event, browser, and layout recording for a virtualized transcript. */
export class HistoryRecorder implements HistoryDiagnostics {
  #recording: ActiveRecording | null = null;
  #sources = new Map<string, LayoutSource>();
  #frame: number | null = null;
  readonly #markPrefix: string;
  readonly #testsEnabled: () => boolean;

  constructor({ markPrefix, testsEnabled = () => false }: HistoryRecorderOptions) {
    this.#markPrefix = markPrefix;
    this.#testsEnabled = testsEnabled;
  }

  mark(event: HistoryEvent): void {
    const recording = this.#recording;
    const testsEnabled = this.#testsEnabled();
    if (!recording && !testsEnabled) return;
    if (recording && recording.marks.length >= MAX_CAPTURE_MARKS) {
      recording.droppedMarks++;
      return;
    }
    const name = `${this.#markPrefix}${event.kind}`;
    const startTime = performance.now();
    performance.mark(name, { detail: event });
    recording?.marks.push({ name, startTime, detail: event });
  }

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
    const recording: ActiveRecording = {
      startedAt: new Date().toISOString(),
      droppedMarks: 0,
      marks: [],
      droppedBrowser: 0,
      browser: [],
      observers: [],
      layouts: [],
      droppedLayouts: 0,
      previousFrames: new Map<string, string>(),
    };
    this.#recording = recording;
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
      recording.droppedBrowser++;
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

  stopRecording(): HistoryRecording | null {
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
    if (!this.#testsEnabled()) {
      for (const name of new Set(recording.marks.map(({ name }) => name))) performance.clearMarks(name);
    }
    return {
      version: 4,
      startedAt: recording.startedAt,
      savedAt: new Date().toISOString(),
      timeOrigin: performance.timeOrigin,
      viewport: { width: window.innerWidth, height: window.innerHeight, devicePixelRatio: window.devicePixelRatio },
      dropped: recording.droppedMarks + recording.droppedBrowser,
      marks: recording.marks,
      browser: recording.browser,
      layouts: recording.layouts,
      droppedLayouts: recording.droppedLayouts,
    };
  }
}

/** Saves a recording on this device. The JSON can contain row keys and other identifying metadata. */
export function downloadHistoryRecording(recording: HistoryRecording, filePrefix: string): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(recording, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `${filePrefix}-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
