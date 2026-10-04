// What a reader of the thread history sees, frame by frame, for tests that measure it. Installed
// into every page before it loads (history_probe.py); read back through window.__historyProbe.
//
// The history positions its rows with `transform`, which the browser's layout-instability API
// does not count, so this also compares what each frame painted with the frame before it. A frame
// is read in the task after its animation callbacks, style, layout and resize-observer delivery
// have run, which is when the browser has committed it: a `requestAnimationFrame` callback runs
// before all of those and would report a state the frame never shows.
(() => {
  const AREA = '[aria-label="Thread history"]';
  const ROW = "[data-thread-anchor]";
  const LONG_FRAME_MS = 50;
  const MAX_EVENTS = 3000;
  const STORAGE_KEY = "historyProbe";
  // Rows that touch are normal; more than this apart or overlapping is a layout that has not caught up.
  const TOLERANCE_PX = 1;

  const probe = {
    navigatedAt: performance.timeOrigin,
    frames: 0,
    framesWithRows: 0,
    longFrames: 0,
    maxFrameGapMs: 0,
    firstRowsAt: null,
    firstSettledAt: null,
    settledFlips: 0,
    inconsistentFrames: 0,
    longestInconsistentRun: 0,
    maxOverlapPx: 0,
    maxGapPx: 0,
    shiftFrames: 0,
    shiftFramesWhileSettled: 0,
    maxShiftPx: 0,
    sumShiftPx: 0,
    layoutShiftSum: 0,
    layoutShifts: [],
    syncResponses: [],
    events: [],
  };

  let last = null;
  let settled = false;
  let inconsistentRun = 0;
  let previousTime = null;

  const describe = (node) => {
    if (!(node instanceof Element)) return "";
    const row = node.closest(ROW);
    return row ? `row ${row.dataset.threadAnchor}` : node.tagName.toLowerCase();
  };

  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (entry.hadRecentInput) continue;
      probe.layoutShiftSum += entry.value;
      if (probe.layoutShifts.length < 500) {
        probe.layoutShifts.push({
          t: entry.startTime,
          value: entry.value,
          sources: (entry.sources ?? []).map((source) => describe(source.node)),
        });
      }
    }
  }).observe({ type: "layout-shift", buffered: true });

  // When the page's reads of rows and bodies finished, on the clock `HistoryTrace` stamps events
  // with, to tell a layout change that follows data arriving from one that does not.
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      const { pathname } = new URL(entry.name);
      if (pathname.includes("/sync/") && probe.syncResponses.length < MAX_EVENTS) {
        probe.syncResponses.push({ started: entry.startTime, finished: entry.responseEnd, path: pathname });
      }
    }
  }).observe({ type: "resource", buffered: true });

  const frameTasks = new MessageChannel();
  const frame = (now) => {
    requestAnimationFrame(frame);
    frameTasks.port2.postMessage(now);
  };
  frameTasks.port1.onmessage = (message) => committed(message.data);

  const committed = (now) => {
    probe.frames++;
    if (previousTime !== null) {
      const gap = now - previousTime;
      probe.maxFrameGapMs = Math.max(probe.maxFrameGapMs, gap);
      if (gap > LONG_FRAME_MS) probe.longFrames++;
    }
    previousTime = now;

    const area = document.querySelector(AREA);
    if (!area) {
      last = null;
      return;
    }
    const rows = [...area.querySelectorAll(ROW)];
    if (rows.length === 0) {
      last = null;
      return;
    }
    probe.framesWithRows++;
    probe.firstRowsAt ??= now;

    const areaRect = area.getBoundingClientRect();
    const rects = rows.map((row) => row.getBoundingClientRect());
    let overlapPx = 0;
    let gapPx = 0;
    for (let index = 1; index < rects.length; index++) {
      const between = rects[index].top - rects[index - 1].bottom;
      if (between < -TOLERANCE_PX) overlapPx += -between;
      else if (between > TOLERANCE_PX) gapPx += between;
    }
    const inconsistent = overlapPx > 0 || gapPx > 0;
    inconsistentRun = inconsistent ? inconsistentRun + 1 : 0;
    if (inconsistent) probe.inconsistentFrames++;
    probe.longestInconsistentRun = Math.max(probe.longestInconsistentRun, inconsistentRun);
    probe.maxOverlapPx = Math.max(probe.maxOverlapPx, overlapPx);
    probe.maxGapPx = Math.max(probe.maxGapPx, gapPx);

    // Content that moved on screen by more than scrolling explains, relative to a row that was
    // already the first one visible in the previous frame.
    let shift = 0;
    if (last) {
      const index = rows.findIndex((row) => row.dataset.threadAnchor === last.key);
      if (index >= 0) {
        shift = rects[index].top - areaRect.top - last.offset + (area.scrollTop - last.scrollTop);
      }
    }
    const firstVisible = rects.findIndex((rect) => rect.bottom > areaRect.top);
    last =
      firstVisible >= 0
        ? {
            key: rows[firstVisible].dataset.threadAnchor,
            offset: rects[firstVisible].top - areaRect.top,
            scrollTop: area.scrollTop,
          }
        : null;

    const nowSettled = area.dataset.layoutSettled === "true";
    if (nowSettled !== settled) {
      settled = nowSettled;
      probe.settledFlips++;
      if (settled) probe.firstSettledAt ??= now;
    }
    const shifted = Math.abs(shift) > TOLERANCE_PX;
    if (shifted) {
      probe.shiftFrames++;
      probe.sumShiftPx += Math.abs(shift);
      probe.maxShiftPx = Math.max(probe.maxShiftPx, Math.abs(shift));
      if (settled) probe.shiftFramesWhileSettled++;
    }
    if ((shifted || inconsistent) && probe.events.length < MAX_EVENTS) {
      probe.events.push({
        t: now,
        scrollTop: area.scrollTop,
        scrollHeight: area.scrollHeight,
        shift,
        overlapPx,
        gapPx,
        settled,
        mode: area.dataset.scrollMode,
        rows: rows.length,
      });
    }
  };
  requestAnimationFrame(frame);

  // How far off the virtualizer's layout of each row was before the row was first read, in
  // pixels: for rows it laid out from a remembered earlier reading and for rows it only guessed.
  const errorStats = (all) => {
    const errors = all.map((entry) => Math.abs(entry.error)).sort((a, b) => a - b);
    const at = (fraction) => errors[Math.min(errors.length - 1, Math.floor(errors.length * fraction))] ?? 0;
    return {
      rows: errors.length,
      meanAbs: errors.length ? errors.reduce((sum, error) => sum + error, 0) / errors.length : 0,
      p50: at(0.5),
      p90: at(0.9),
      p99: at(0.99),
      over50: errors.filter((error) => error > 50).length,
    };
  };
  const estimateErrors = () => {
    const all = window.agentplaneHistoryEstimateErrors?.() ?? [];
    return {
      guessed: errorStats(all.filter((entry) => !entry.remembered)),
      remembered: errorStats(all.filter((entry) => entry.remembered)),
    };
  };

  const summary = (withEvents) => ({
    ...probe,
    events: withEvents ? probe.events : probe.events.length,
    estimateErrors: estimateErrors(),
  });

  // A reload ends this page's probe; keep what it saw for whoever reads the tab's results.
  addEventListener("pagehide", () => {
    try {
      const earlier = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...earlier, summary(false)]));
    } catch (error) {
      console.warn("history probe could not keep its results across the reload", error);
    }
  });

  window.__historyProbe = {
    collect: () => {
      const earlier = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? "[]");
      return [...earlier, summary(true)];
    },
  };
})();
