import { Button, Paper, Text } from "@mantine/core";
import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import { IconArrowDown } from "../icons";
import {
  type JSX,
  type ReactNode,
  useCallback,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { type ThreadEntity, type ThreadWindow } from "./thread_sync";
import { rowKey, type HistoryRow } from "./history_rows";
import { HistoryRowView } from "./history_row_views";
import { revealEvidenceOnTap } from "./thread_evidence";
import { LayoutSettle, markThreadViewEvent, type FollowReason } from "./thread_view_timing";
import { rememberRowHeight, rememberedRowHeight } from "./history_sizes";
import { DISCLOSURE_STICKY_Z_INDEX } from "../disclosure";

type ReadingAnchor = { key: string; offset: number; target?: HTMLElement };

// Collapsing a block hides its own sticky heading (`display: none`): still connected, but with no box to
// hold. Its rect reads as zeros, so a correction toward it is the same step on every resize and never arrives.
const isHidden = (target: HTMLElement): boolean => target.isConnected && target.getClientRects().length === 0;

// How close the top of the loaded rows comes to the viewport's before the page before them loads:
// a full screen, so the load lands before the reader can actually see the top -- reading up
// through a long thread feels like an ordinary lazy-loaded scroll, not a stop-and-wait at the edge.
//
// This makes eager pagination genuinely viewport-height-relative: shrinking chrome elsewhere on the
// page (the topbar, the composer) makes this taller, so the same thread settles one page further
// into its backlog. PR #8308's topbar change tripped this on two E2E tests that assumed a fixed
// fetch count -- not a flake, just this threshold moving. A test asserting an exact older-page fetch
// count, or relying on a fixed-size synthetic backlog outlasting a fixed number of scroll-to-top
// cycles, is coupled to this and needs headroom (see test_thread_window_browser.py's two tests fixed
// there) rather than an assumption pinned to today's chrome height.
const loadOlderWithin = (element: HTMLDivElement): number => element.clientHeight;

// What the virtualizer lays an unmeasured row out with, until it is mounted and read.
const ESTIMATED_ROW_HEIGHT = 180;

export interface VirtualizedHistoryProps {
  threadId: string;
  rows: HistoryRow[];
  inputEchoes: ReadonlyMap<string, string>;
  onInputEchoLoaded: (commandId: string) => void;
  /** Local input without an admission cursor yet: shown provisionally after ordered history. */
  tail?: ReactNode;
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder" | "connection">;
}

export function VirtualizedHistory({
  threadId,
  rows,
  inputEchoes,
  onInputEchoLoaded,
  tail,
  running,
  activeTurn,
  history,
}: VirtualizedHistoryProps): JSX.Element {
  useEffect(() => {
    markThreadViewEvent({
      kind: "older-state",
      loading: history.loadingOlder,
      available: history.olderAvailable,
      rowCount: rows.length,
    });
  }, [history.loadingOlder, history.olderAvailable, rows.length]);
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const tailContent = useRef<HTMLDivElement>(null);
  const endOfHistory = useRef<HTMLDivElement>(null);
  // Whether the end of the history is on screen, so the reader can see it is not following it.
  const [endVisible, setEndVisible] = useState(true);
  const atBottom = useRef(true);
  const layoutSettle = useRef<LayoutSettle | null>(null);
  const lastTotalSize = useRef(0);
  // Each row's last measured height, to tell a row's first reading from a resize of it, and what
  // the virtualizer laid each unmeasured row out with.
  const measuredHeights = useRef(new Map<string, number>());
  const estimatedHeights = useRef(new Map<string, { height: number; remembered: boolean }>());
  const previousScrollTop = useRef(0);
  // Every bottom the viewport has had since the last scroll event or content resize was handled.
  // A return to the bottom lands on whichever one was current when it ran; a card can grow in
  // that task or an earlier one before the browser dispatches the scroll event.
  const recentBottoms = useRef<number[]>([]);
  // Where a click on a disclosure left the reader, until they next scroll: standing there, at
  // whatever bottom the history has recorded, is their place and not a return to the bottom.
  const clickedAt = useRef<number | null>(null);
  const pointerScrolling = useRef(false);
  const captureNextScroll = useRef(false);
  const scrolledSinceInput = useRef(false);
  const touchY = useRef<number | null>(null);
  const restorationFrame = useRef<number | null>(null);
  const restoringAnchor = useRef<string | null>(null);
  const restorationSize = useRef<number | null>(null);
  const previousCount = useRef(rows.length);
  const previousFirstKey = useRef<string | null>(null);
  const readingAnchor = useRef<ReadingAnchor | null>(null);
  // Keep a nested sticky row rendered while collapsing it can shrink the row out of the
  // virtual window. A later scroll gesture releases it.
  const [stickyAnchorKey, setStickyAnchorKey] = useState<string | null>(null);
  const [stickyAnchorRevision, setStickyAnchorRevision] = useState(0);
  const stickyAnchorPending = useRef<string | null>(null);
  // Widened around a just-landed older page so every one of its rows mounts and measures in the
  // same pass, rather than progressively as scrolling reveals more of it -- each of *those* later
  // corrections is itself a visible, uncalled-for jump (see restoreAnchor/restoringScroll below).
  const [pageOverscan, setPageOverscan] = useState(0);
  // Bumped once per page landed above the reader; drives the effect that measures it (widened)
  // before restoreAnchor ever runs for it.
  const [pagePrepended, setPagePrepended] = useState(0);
  // The row holding the entity a reading anchor was taken at. A run keeps its key while steps
  // stream into it, but gains a new first step when older history loads into it.
  const anchorIndex = (key: string): number =>
    rows.findIndex((row) => row.entities.some((entity) => `${entity.entityKind}:${entity.entityId}` === key));
  const stickyAnchorIndex = stickyAnchorKey === null ? -1 : anchorIndex(stickyAnchorKey);
  const rangeExtractor = useCallback(
    (range: Parameters<typeof defaultRangeExtractor>[0]) => {
      const indexes = defaultRangeExtractor(range);
      if (stickyAnchorIndex < 0 || indexes.includes(stickyAnchorIndex)) return indexes;
      return [...indexes, stickyAnchorIndex].sort((left, right) => left - right);
    },
    [stickyAnchorIndex]
  );
  const anchorElement = (key: string): HTMLElement | null => {
    const index = anchorIndex(key);
    if (index < 0) return null;
    return (
      viewport.current?.querySelector<HTMLElement>(`[data-thread-anchor="${rows[index].entities[0].cursor}"]`) ?? null
    );
  };
  const recordBottom = (element: HTMLDivElement) => {
    const bottom = element.scrollHeight - element.clientHeight;
    if (!recentBottoms.current.includes(bottom)) recentBottoms.current.push(bottom);
  };
  // Restoring to a row that is not mounted scrolls to its estimated offset. When the rows above it
  // measure shorter than estimated, that offset lies past the end and the browser clamps it: the
  // restoration's own scroll lands on the bottom, which is not the reader returning there.
  const restoringScroll = () => restorationFrame.current !== null;
  // What tests and a reader-position investigation read of the history's state, from the region.
  const publishMode = useCallback(() => {
    const element = viewport.current;
    if (element)
      element.dataset.scrollMode = atBottom.current ? "following" : restoringScroll() ? "restoring" : "reading";
  }, []);
  const cancelRestoration = useCallback(() => {
    if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
    restorationFrame.current = null;
    restorationSize.current = null;
    restoringAnchor.current = null;
    publishMode();
  }, [publishMode]);
  const setFollowing = useCallback(
    (following: boolean, reason: FollowReason) => {
      if (atBottom.current === following) return;
      atBottom.current = following;
      markThreadViewEvent({ kind: "follow", following, reason });
      publishMode();
    },
    [publishMode]
  );
  const layoutChanged = () => layoutSettle.current?.changed();
  const followPreviousBottom = useCallback(
    (element: HTMLDivElement) => {
      // A programmatic return to the old bottom can be delivered after a card grows. Preserve
      // it before restoring a stale reader anchor, while an explicit user gesture owns its scroll,
      // as does a restoration still settling.
      if (
        captureNextScroll.current ||
        restoringScroll() ||
        readingAnchor.current?.target?.isConnected ||
        element.scrollTop === clickedAt.current
      ) {
        return false;
      }
      if (!recentBottoms.current.some((bottom) => Math.abs(element.scrollTop - bottom) <= 2)) return false;
      setFollowing(true, "returned-to-previous-bottom");
      cancelRestoration();
      element.scrollTop = element.scrollHeight;
      return true;
    },
    [cancelRestoration, setFollowing]
  );
  function correctRestoration(): number | null {
    const anchor = readingAnchor.current;
    const element = viewport.current;
    if (!anchor || !element || restoringAnchor.current !== anchor.key) return null;
    const row = anchorElement(anchor.key);
    if (!row) return null;
    if (anchor.target && isHidden(anchor.target)) {
      captureReadingAnchor(element);
      return 0;
    }
    const anchorTarget = anchor.target?.isConnected ? anchor.target : row;
    const correction = anchorTarget.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
    element.scrollTop += correction;
    return correction;
  }
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => viewport.current,
    estimateSize: (index) => {
      const key = rowKey(rows[index]);
      const remembered = rememberedRowHeight(threadId, viewport.current?.clientWidth ?? 0, key);
      const height = remembered ?? ESTIMATED_ROW_HEIGHT;
      estimatedHeights.current.set(key, { height, remembered: remembered !== undefined });
      return height;
    },
    getItemKey: (index) => rowKey(rows[index]),
    rangeExtractor,
    measureElement: (element) => {
      const measured = element.getBoundingClientRect().height;
      const row = rows[Number(element.getAttribute("data-index"))];
      if (row) {
        const key = rowKey(row);
        const previous = measuredHeights.current.get(key);
        if (previous === undefined || Math.abs(previous - measured) >= 1) {
          markThreadViewEvent({
            kind: "measure",
            key,
            estimate: previous ?? estimatedHeights.current.get(key)?.height ?? ESTIMATED_ROW_HEIGHT,
            measured,
            first: previous === undefined,
            remembered: previous === undefined && estimatedHeights.current.get(key)?.remembered === true,
          });
          measuredHeights.current.set(key, measured);
          rememberRowHeight(threadId, viewport.current?.clientWidth ?? 0, key, measured);
        }
      }
      return measured;
    },
    overscan: 5 + pageOverscan,
    onChange: (instance, sync) => {
      const total = instance.getTotalSize();
      if (total !== lastTotalSize.current) {
        markThreadViewEvent({
          kind: "virtual-size",
          before: lastTotalSize.current,
          after: total,
          sync,
          following: atBottom.current,
        });
        lastTotalSize.current = total;
        layoutChanged();
      }
      // A card can resize before virtual-core applies its measured transform. Wait for
      // that measurement rather than guessing how many animation frames it requires.
      if (viewport.current && followPreviousBottom(viewport.current)) return;
      if (atBottom.current) {
        restorationSize.current = null;
        restoringAnchor.current = null;
        return;
      }
      if (sync || restorationSize.current === null || instance.getTotalSize() === restorationSize.current) return;
      restorationSize.current = null;
      if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
      restorationFrame.current = requestAnimationFrame(() => {
        restorationFrame.current = null;
        if (viewport.current && followPreviousBottom(viewport.current)) return;
        if (correctRestoration() !== null) {
          restoringAnchor.current = null;
        }
      });
    },
  });
  // ResizeObserver below preserves the first visible row explicitly. This is an
  // instance hook in the pinned virtual-core version, rather than an option.
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = () => false;
  const expectUserScroll = () => {
    setStickyAnchorKey(null);
    if (readingAnchor.current?.target) {
      readingAnchor.current = { key: readingAnchor.current.key, offset: readingAnchor.current.offset };
    }
    if (captureNextScroll.current) return;
    captureNextScroll.current = true;
    scrolledSinceInput.current = false;
  };
  // A gesture asks for the page before the oldest row as its scroll events reach the top. This asks
  // where no scroll event will: rows too few to scroll, a gesture that ended at the top, a page that
  // landed with the reader still there. A gesture or restoration in progress has not settled where
  // the reader is, and until the tail shows there is no top to reach.
  const loadOlderAtTop = useEffectEvent(() => {
    const element = viewport.current;
    if (
      element &&
      rows.length > 0 &&
      history.olderAvailable &&
      !captureNextScroll.current &&
      restoringAnchor.current === null &&
      element.scrollTop < loadOlderWithin(element)
    )
      history.loadOlder();
  });
  const captureReadingAnchor = useCallback(
    (element: HTMLDivElement, clickedTarget?: HTMLElement) => {
      const viewportTop = element.getBoundingClientRect().top;
      const first = [...element.querySelectorAll<HTMLElement>("[data-thread-anchor]")].find(
        (candidate) => candidate.getBoundingClientRect().bottom > viewportTop
      );
      const clickedRow = clickedTarget?.closest<HTMLElement>("[data-thread-anchor]");
      const anchorRowElement = clickedRow ?? first;
      const anchorRow = anchorRowElement
        ? rows.find((row) => row.entities[0].cursor.toString() === anchorRowElement.dataset.threadAnchor)
        : undefined;
      const clickedHeading = clickedTarget?.closest<HTMLElement>(".agentplane-disclosure-heading");
      const stickyTop = clickedHeading ? Number.parseFloat(getComputedStyle(clickedHeading).top) : Number.NaN;
      const headingOffset = clickedHeading ? clickedHeading.getBoundingClientRect().top - viewportTop : Number.NaN;
      const clickedHeadingIsSticky = Number.isFinite(stickyTop) && Math.abs(headingOffset - stickyTop) <= 2;
      const anchorTarget =
        clickedTarget && clickedRow?.contains(clickedTarget) && clickedHeadingIsSticky
          ? clickedTarget
          : anchorRowElement;
      if (anchorRowElement && anchorRow && anchorTarget) {
        const anchor = {
          key: rowKey(anchorRow),
          offset: anchorTarget.getBoundingClientRect().top - viewportTop,
          ...(anchorTarget !== anchorRowElement ? { target: anchorTarget } : {}),
        };
        readingAnchor.current = anchor;
        if (clickedTarget) stickyAnchorPending.current = clickedHeadingIsSticky ? anchor.key : null;
        markThreadViewEvent({ kind: "anchor", key: anchor.key, offset: anchor.offset });
      }
    },
    [rows]
  );
  useLayoutEffect(() => {
    const anchor = readingAnchor.current;
    const element = viewport.current;
    const target = anchor?.target;
    if (!anchor || anchor.key !== stickyAnchorKey || !element || !target?.isConnected) return;
    if (isHidden(target)) {
      captureReadingAnchor(element);
      return;
    }

    // A nested disclosure can shrink its containing virtual row past the scroll position. Restore
    // its sticky control in this commit, before the browser paints it clamped by that row's bottom.
    cancelRestoration();
    restoringAnchor.current = anchor.key;
    const correction = target.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
    element.scrollTop += correction;
    restorationFrame.current = requestAnimationFrame(() => {
      if (restoringAnchor.current === anchor.key) restoringAnchor.current = null;
      restorationFrame.current = null;
      publishMode();
    });
    publishMode();
  }, [cancelRestoration, captureReadingAnchor, publishMode, stickyAnchorKey, stickyAnchorRevision]);
  const restoreAnchor = useEffectEvent((anchor: ReadingAnchor, awaitMeasurement = false) => {
    const index = anchorIndex(anchor.key);
    if (index < 0) return;
    cancelRestoration();
    layoutChanged();
    restoringAnchor.current = anchor.key;
    const correctFromDom = (): number | null => {
      const element = viewport.current;
      const row = anchorElement(anchor.key);
      if (!element || !row) return null;
      if (anchor.target && isHidden(anchor.target)) {
        captureReadingAnchor(element);
        return 0;
      }
      const anchorTarget = anchor.target?.isConnected ? anchor.target : row;
      const currentOffset = anchorTarget.getBoundingClientRect().top - element.getBoundingClientRect().top;
      const correction = currentOffset - anchor.offset;
      element.scrollTop += correction;
      return correction;
    };
    const correction = correctFromDom();
    markThreadViewEvent({ kind: "restore", key: anchor.key, correction });
    if (correction === null) virtualizer.scrollToIndex(index, { align: "start" });
    // Waiting for measurement assumes the row is mounted and in place. One scrolled to by its
    // estimate needs the frames, whose pending state keeps a clamped scroll from reading as the bottom.
    if (awaitMeasurement && correction !== null && Math.abs(correction) <= 2) {
      restorationSize.current = virtualizer.getTotalSize();
      return;
    }
    restorationFrame.current = requestAnimationFrame(() => {
      if (restoringAnchor.current === anchor.key) correctFromDom();
      restorationFrame.current = requestAnimationFrame(() => {
        if (restoringAnchor.current === anchor.key) restoringAnchor.current = null;
        restorationFrame.current = null;
        publishMode();
      });
    });
    publishMode();
  });
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    // A row that is still loading its text grows when the text arrives, which is a layout change not yet seen.
    const loading = () => element.querySelector('[aria-busy="true"]') !== null;
    const settle = new LayoutSettle(
      () => restoringScroll() || loading(),
      (settled) => {
        element.dataset.layoutSettled = String(settled);
        markThreadViewEvent({ kind: "settled", settled });
      }
    );
    layoutSettle.current = settle;
    element.dataset.layoutSettled = "false";
    publishMode();
    return () => {
      settle.dispose();
      layoutSettle.current = null;
    };
  }, [publishMode]);
  useLayoutEffect(() => {
    const element = viewport.current;
    const firstKey = rows[0] ? rowKey(rows[0]) : null;
    if (rows.length !== previousCount.current) layoutChanged();
    if (element && atBottom.current && rows.length > previousCount.current) element.scrollTop = element.scrollHeight;
    if (
      element &&
      !atBottom.current &&
      rows.length > 0 &&
      readingAnchor.current &&
      (previousCount.current === 0 || previousFirstKey.current !== firstKey)
    ) {
      const added = rows.length - previousCount.current;
      if (added > 0 && previousCount.current > 0) {
        // A page landed above the reader. Widen overscan to mount and measure all of it (next
        // effect below, once this commit lands) before restoreAnchor ever runs for it -- rather
        // than letting restoreAnchor guess via estimateSize now and chase a correction once the
        // real heights are known.
        markThreadViewEvent({ kind: "prepend", added });
        setPageOverscan((current) => Math.max(current, added));
        setPagePrepended((current) => current + 1);
      } else {
        restoreAnchor(readingAnchor.current);
      }
    }
    previousCount.current = rows.length;
    previousFirstKey.current = firstKey;
  }, [rows, virtualizer]);
  // pagePrepended is a monotonic counter, not derived from pageOverscan's value, so a second page
  // landing while the first's widened mount hasn't narrowed back yet still triggers this.
  useLayoutEffect(() => {
    if (pagePrepended === 0) return;
    if (readingAnchor.current) restoreAnchor(readingAnchor.current);
    // Narrow back once this settles -- measurement itself is synchronous on mount, but
    // restoreAnchor's own correction can still take a couple of frames to land.
    let frame: number;
    const tick = () => {
      if (restoringScroll()) {
        frame = requestAnimationFrame(tick);
        return;
      }
      setPageOverscan(0);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [pagePrepended]);
  useLayoutEffect(() => {
    const element = viewport.current;
    const content = contents.current;
    if (!element || !content) return;
    recordBottom(element);
    const observer = new ResizeObserver(() => {
      layoutChanged();
      // A scrollbar drag or programmatic equivalent can reach the old bottom in the same task
      // that grows the last card, before the browser dispatches its scroll event. Preserve that
      // user choice across the resize without interpreting arbitrary layout movement as intent.
      const pinned = followPreviousBottom(element) || atBottom.current;
      markThreadViewEvent({ kind: "resize", scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, pinned });
      if (pinned) {
        element.scrollTop = element.scrollHeight;
        // This component-owned bottom correction may dispatch its scroll event after another
        // row measurement changes the bottom. Compare that event with the corrected position,
        // not the stale position from the last scroll event.
        previousScrollTop.current = element.scrollTop;
      }
      // Content can resize while a wheel, touch, or key scroll is still settling. Its
      // measured rows do not describe the reader's final position yet; scrollend will
      // capture that position before a later resize restoration is eligible.
      else if (!captureNextScroll.current && readingAnchor.current) restoreAnchor(readingAnchor.current, true);
      recentBottoms.current = [element.scrollHeight - element.clientHeight];
    });
    observer.observe(content);
    if (tailContent.current) observer.observe(tailContent.current);
    // A body arriving for a remounted or streaming card is a DOM mutation, and the scroll event
    // of a return to the bottom that follows it in the same frame precedes the ResizeObserver
    // delivery for it. The mutation callback runs before any later task, so record its bottom.
    const mutations = new MutationObserver(() => recordBottom(element));
    mutations.observe(content, { subtree: true, childList: true, characterData: true, attributes: true });
    if (tailContent.current)
      mutations.observe(tailContent.current, { subtree: true, childList: true, characterData: true, attributes: true });
    return () => {
      observer.disconnect();
      mutations.disconnect();
    };
  }, [followPreviousBottom, rows, tail, virtualizer]);
  useEffect(() => {
    const element = viewport.current;
    const end = endOfHistory.current;
    if (!element || !end) return;
    // The bottom margin is the slack within which a reader still counts as following.
    const observer = new IntersectionObserver(([entry]) => setEndVisible(entry.isIntersecting), {
      root: element,
      rootMargin: "0px 0px 24px 0px",
    });
    observer.observe(end);
    return () => observer.disconnect();
  }, []);
  const jumpToLatest = () => {
    const element = viewport.current;
    if (!element) return;
    setFollowing(true, "jump-to-latest");
    setStickyAnchorKey(null);
    clickedAt.current = null;
    cancelRestoration();
    element.scrollTop = element.scrollHeight;
  };
  // The observers above re-subscribe on every render's rows; a restoration in flight outlives that.
  useLayoutEffect(() => cancelRestoration, [cancelRestoration]);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const onScrollEnd = () => {
      markThreadViewEvent({
        kind: "scrollend",
        restoring: restoringAnchor.current !== null,
        capturing: captureNextScroll.current,
      });
      if (restoringAnchor.current !== null || !captureNextScroll.current) return;
      captureReadingAnchor(element);
      captureNextScroll.current = false;
      loadOlderAtTop();
    };
    element.addEventListener("scrollend", onScrollEnd);
    return () => element.removeEventListener("scrollend", onScrollEnd);
  }, [captureReadingAnchor]);
  useEffect(() => {
    loadOlderAtTop();
  });
  return (
    <div
      ref={viewport}
      role="region"
      aria-label="Thread history"
      tabIndex={0}
      style={{ overflowY: "auto", overflowAnchor: "none", flex: 1, minHeight: 0 }}
      onWheel={(event) => {
        cancelRestoration();
        const element = event.currentTarget;
        markThreadViewEvent({
          kind: "input",
          source: "wheel",
          direction: event.deltaY < 0 ? "up" : "down",
          scrollTop: element.scrollTop,
        });
        const canScroll =
          (event.deltaY < 0 && element.scrollTop > 0) ||
          (event.deltaY > 0 && element.scrollTop < element.scrollHeight - element.clientHeight);
        if (canScroll) {
          expectUserScroll();
          if (event.deltaY < 0) setFollowing(false, "wheel-up");
        } else if (!scrolledSinceInput.current) {
          captureNextScroll.current = false;
        }
      }}
      onKeyDown={(event) => {
        cancelRestoration();
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
          markThreadViewEvent({
            kind: "input",
            source: "key",
            direction: ["ArrowUp", "PageUp", "Home"].includes(event.key) ? "up" : "down",
            scrollTop: event.currentTarget.scrollTop,
          });
        }
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) expectUserScroll();
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) setFollowing(false, "key-up");
      }}
      onKeyUp={() => {
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onPointerDown={(event) => {
        markThreadViewEvent({
          kind: "input",
          source: "pointer",
          direction: "unknown",
          scrollTop: event.currentTarget.scrollTop,
        });
        cancelRestoration();
        pointerScrolling.current = true;
        expectUserScroll();
      }}
      onPointerUp={() => {
        pointerScrolling.current = false;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onPointerCancel={() => {
        pointerScrolling.current = false;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onTouchStart={(event) => {
        cancelRestoration();
        touchY.current = event.touches[0]?.clientY ?? null;
      }}
      onTouchMove={(event) => {
        const next = event.touches[0]?.clientY;
        markThreadViewEvent({
          kind: "input",
          source: "touch",
          direction:
            next !== undefined && touchY.current !== null ? (next > touchY.current ? "up" : "down") : "unknown",
          scrollTop: event.currentTarget.scrollTop,
        });
        expectUserScroll();
        if (next !== undefined && touchY.current !== null && next > touchY.current) setFollowing(false, "touch-up");
        touchY.current = next ?? null;
      }}
      onTouchEnd={() => {
        touchY.current = null;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onClickCapture={(event) => {
        // Opening or closing a row resizes it under the line the reader clicked. Following the tail
        // would pin the bottom instead, carrying that line away; so a click on a disclosure stops
        // following, and adopts the place as it stands before the row moves. A history that does
        // not scroll has no place to lose, and goes on following as it grows.
        const element = event.currentTarget;
        if (!(event.target instanceof Element)) return;
        const clickedTarget = event.target.closest<HTMLElement>("[aria-expanded]");
        if (!clickedTarget) return;
        if (element.scrollHeight > element.clientHeight) {
          setFollowing(false, "disclosure-click");
          clickedAt.current = element.scrollTop;
        }
        captureReadingAnchor(element, clickedTarget);
        markThreadViewEvent({
          kind: "click",
          scrollTop: element.scrollTop,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
        });
      }}
      onClick={(event) => {
        revealEvidenceOnTap(event);
        const key = stickyAnchorPending.current;
        stickyAnchorPending.current = null;
        if (key === null) return;
        setStickyAnchorKey(key);
        setStickyAnchorRevision((revision) => revision + 1);
      }}
      onScroll={(event) => {
        const element = event.currentTarget;
        // A disclosure's anchor restoration can emit scroll after the click handler ends. Keep
        // the click guard through those programmatic corrections; only a later user gesture clears it.
        if (captureNextScroll.current && element.scrollTop !== clickedAt.current) clickedAt.current = null;
        const followed = followPreviousBottom(element);
        recentBottoms.current = [element.scrollHeight - element.clientHeight];
        markThreadViewEvent({
          kind: "scroll",
          scrollTop: element.scrollTop,
          previousTop: previousScrollTop.current,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
          followed,
        });
        if (followed) {
          previousScrollTop.current = element.scrollTop;
          return;
        }
        const movedUp = element.scrollTop < previousScrollTop.current;
        const movedDown = element.scrollTop > previousScrollTop.current;
        const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        if (movedUp) {
          setFollowing(false, "scrolled-up");
        } else if (!restoringScroll() && nearBottom && (atBottom.current || movedDown)) {
          setFollowing(true, "scrolled-to-bottom");
          cancelRestoration();
        }
        previousScrollTop.current = element.scrollTop;
        if (restoringAnchor.current !== null) return;
        if (!captureNextScroll.current && !pointerScrolling.current && touchY.current === null) return;
        captureNextScroll.current = true;
        scrolledSinceInput.current = true;
        // Keep the anchor current through the gesture, not just once it settles at scrollend: an
        // older page can land, and prepend rows, while this gesture is still moving. Restoring to
        // a stale anchor from an earlier, already-settled gesture would pull the reader back to
        // where they were reading before, not where this gesture has since taken them.
        captureReadingAnchor(element);
        if (element.scrollTop < loadOlderWithin(element)) {
          markThreadViewEvent({ kind: "load-older" });
          history.loadOlder();
        }
      }}
    >
      {history.loadingOlder && (
        // No height of its own: it floats over the rows without moving any of them.
        <div
          style={{
            position: "sticky",
            top: 0,
            height: 0,
            zIndex: 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "flex-start",
            pointerEvents: "none",
          }}
        >
          <Paper role="status" shadow="xs" radius="xl" px="sm" py={2} mt="xs" withBorder>
            <Text size="xs" c="dimmed">
              Loading earlier…
            </Text>
          </Paper>
        </div>
      )}
      <div ref={contents} style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((item) => {
          const row = rows[item.index];
          return row ? (
            <div
              key={item.key}
              data-index={item.index}
              data-thread-anchor={row.entities[0].cursor.toString()}
              ref={virtualizer.measureElement}
              style={{
                // Keep rows in scroll-content coordinates so sticky disclosure descendants track
                // the viewport instead of moving with a per-row transform.
                position: "absolute",
                top: item.start,
                left: 0,
                width: "100%",
                paddingBottom: "var(--mantine-spacing-xs)",
              }}
            >
              <HistoryRowView
                threadId={threadId}
                row={row}
                live={(entity) => running && entity.turnId === activeTurn}
                inputEchoes={inputEchoes}
                onInputEchoLoaded={onInputEchoLoaded}
              />
            </div>
          ) : null;
        })}
      </div>
      {tail && <div ref={tailContent}>{tail}</div>}
      <div ref={endOfHistory} />
      {running && !endVisible && (
        // No height of its own, like the loading indicator: it floats over the rows without moving them.
        <div
          style={{
            position: "sticky",
            bottom: 0,
            height: 0,
            zIndex: DISCLOSURE_STICKY_Z_INDEX + 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "flex-end",
            pointerEvents: "none",
          }}
        >
          <Button
            size="compact-xs"
            variant="default"
            radius="xl"
            mb="xs"
            leftSection={<IconArrowDown size={14} />}
            onClick={jumpToLatest}
            style={{ pointerEvents: "auto" }}
          >
            Jump to latest
          </Button>
        </div>
      )}
    </div>
  );
}
