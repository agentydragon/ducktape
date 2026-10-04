import { ActionIcon, Box, Button, Flex, Group, Menu, Paper, Select, Stack, Text, Textarea } from "@mantine/core";
import { create } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconArrowDown from "@tabler/icons-react/dist/esm/icons/IconArrowDown.mjs";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconHistory from "@tabler/icons-react/dist/esm/icons/IconHistory.mjs";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import IconSend from "@tabler/icons-react/dist/esm/icons/IconSend.mjs";
import {
  type JSX,
  type KeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { CommandSchema } from "../../../protocol/command_pb";
import { ItemKind } from "../../../protocol/event_pb";
import {
  displayableError,
  getThread,
  models,
  modelsForHarness,
  resumeThread,
  type ModelOption,
  type SandboxView,
  type ThreadView,
} from "../client";
import { ComposerPendingActions } from "../actions/affordance";
import { decimalBigInt, useThreadSync, type ThreadEntity, type ThreadWindow } from "./thread_sync";
import {
  historyRows,
  rowKey,
  summarizeLifecycleGroup,
  summarizeRun,
  summarizeSetup,
  type HistoryRow,
} from "./history_rows";
import { liveSandboxesUrl, LiveStatus, useLive, useRequiredThreadsLive, type SandboxesSnapshot } from "../live";
import { StaleNotice, useOptionalStreamStatus, type StreamStatus } from "../stream_status";
import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";
import { CollapsibleCard, EntityCard, ItemStatus, pendingSentMessage } from "./thread_cards";
import {
  PendingInputMessages,
  ProjectedCommandRows,
  SelectedCommandOutcomes,
  useProjectedCommands,
} from "./thread_commands";
import { ChronologicalDebugProvider, useOpenChronologicalDebug } from "./chronological_debug";
import { historyTrace, LayoutSettle, type FollowReason } from "./history_trace";
import { rememberRowHeight, rememberedRowHeight } from "./history_sizes";
import { ThreadTitle } from "./thread_title";
import { ThreadStatusIndicator } from "../thread_status_indicator";
import { snapshotFresh, threadStatusFromSnapshot } from "../thread_status";
import { sandboxReady, sandboxSummary } from "../sandbox_status";
import { TopbarActions, TopbarTitle } from "../topbar";
import { installThreadFavicon } from "../thread_favicon";
import {
  appDocumentTitle,
  CONNECTING_TAB_STATUS,
  threadDocumentTitle,
  type ThreadTabTitleStatus,
} from "../tab_metadata";
import "./projected_session.css";

/** A run of tool calls and reasoning steps, folded behind its summary until opened. */
function CollapsibleRows({
  id,
  summary,
  threadId,
  entities,
  live,
  steps,
}: {
  id: string;
  summary: ReactNode;
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
  /** Whether the rows are one-line steps, packed together, rather than cards. */
  steps: boolean;
}): JSX.Element {
  const [open] = useRetainedDisclosure(id);
  return (
    <CollapsibleCard open={open}>
      <RetainedDisclosure id={id} summary={summary}>
        <Stack gap={steps ? 2 : "xs"} mt={steps ? 2 : "xs"} className={steps ? "agentplane-run-steps" : undefined}>
          {entities.map((entity) => (
            <EntityCard key={entity.entityId} threadId={threadId} entity={entity} live={live(entity)} />
          ))}
        </Stack>
      </RetainedDisclosure>
    </CollapsibleCard>
  );
}

function RunView({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:run`}
      summary={
        <Flex component="span" display="inline-flex" gap="xs" align="center" wrap="wrap">
          <Text size="xs" c="dimmed">
            {summarizeRun(entities)}
          </Text>
          <ItemStatus items={entities} live={entities.some(live)} />
        </Flex>
      }
      threadId={threadId}
      entities={entities}
      live={live}
      steps
    />
  );
}

/** Consecutive mundane lifecycle observations (turn started, harness started, an ordinary turn
 * completion, ...), collapsed to their comma-joined labels. Expanding reveals each one exactly as
 * it reads standing alone, evidence toggle included. */
function LifecycleGroupView({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:lifecycle`}
      summary={
        // Unlike RunView's inline-flex Flex, a plain Text defaults to a block <p> -- inside
        // <summary>, that wraps the label to its own line below the disclosure triangle.
        <Text span size="xs" c="dimmed">
          {summarizeLifecycleGroup(entities)}
        </Text>
      }
      threadId={threadId}
      entities={entities}
      live={live}
      steps={false}
    />
  );
}

function SetupView({
  threadId,
  entities,
  live,
}: {
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const first = entities[0];
  return (
    <CollapsibleRows
      id={`${first.projectionEpoch}:${first.entityKind}:${first.entityId}:setup`}
      summary={
        <Text span size="xs" c="dimmed">
          {summarizeSetup(entities)}
        </Text>
      }
      threadId={threadId}
      entities={entities}
      live={live}
      steps={false}
    />
  );
}

export function HistoryRowView({
  threadId,
  row,
  live,
}: {
  threadId: string;
  row: HistoryRow;
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const [first] = row.entities;
  if (row.kind === "setup") return <SetupView threadId={threadId} entities={row.entities} live={live} />;
  // A lone reasoning step is already a folded block of its own; a lone tool call keeps its run's
  // summary, so every tool call reads the same. A lone lifecycle observation has nothing to
  // group with, so it reads exactly as it always has.
  const lone =
    row.kind === "entity" ||
    (row.entities.length === 1 &&
      (row.kind === "lifecycle_group" || ("kind" in first.state && first.state.kind === ItemKind.REASONING)));
  if (lone) return <EntityCard threadId={threadId} entity={first} live={live(first)} />;
  return row.kind === "run" ? (
    <RunView threadId={threadId} entities={row.entities} live={live} />
  ) : (
    <LifecycleGroupView threadId={threadId} entities={row.entities} live={live} />
  );
}

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

function VirtualizedHistory({
  threadId,
  rows,
  tail,
  running,
  activeTurn,
  history,
}: {
  threadId: string;
  rows: HistoryRow[];
  /** Local input without an admission cursor yet: shown provisionally after ordered history. */
  tail?: ReactNode;
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
}): JSX.Element {
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
  const readingAnchor = useRef<{ key: string; offset: number } | null>(null);
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
      historyTrace.record({ kind: "follow", following, reason });
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
      if (captureNextScroll.current || restoringScroll() || element.scrollTop === clickedAt.current) return false;
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
    const correction = row.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
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
    measureElement: (element) => {
      const measured = element.getBoundingClientRect().height;
      const row = rows[Number(element.getAttribute("data-index"))];
      if (row) {
        const key = rowKey(row);
        const previous = measuredHeights.current.get(key);
        if (previous === undefined || Math.abs(previous - measured) >= 1) {
          historyTrace.record({
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
    (element: HTMLDivElement) => {
      const viewportTop = element.getBoundingClientRect().top;
      const first = [...element.querySelectorAll<HTMLElement>("[data-thread-anchor]")].find(
        (candidate) => candidate.getBoundingClientRect().bottom > viewportTop
      );
      const firstRow = first
        ? rows.find((row) => row.entities[0].cursor.toString() === first.dataset.threadAnchor)
        : undefined;
      if (first && firstRow) {
        readingAnchor.current = { key: rowKey(firstRow), offset: first.getBoundingClientRect().top - viewportTop };
        historyTrace.record({ kind: "anchor", ...readingAnchor.current });
      }
    },
    [rows]
  );
  const restoreAnchor = useEffectEvent((anchor: { key: string; offset: number }, awaitMeasurement = false) => {
    const index = anchorIndex(anchor.key);
    if (index < 0) return;
    cancelRestoration();
    layoutChanged();
    restoringAnchor.current = anchor.key;
    const correctFromDom = (): number | null => {
      const element = viewport.current;
      const row = anchorElement(anchor.key);
      if (!element || !row) return null;
      const currentOffset = row.getBoundingClientRect().top - element.getBoundingClientRect().top;
      const correction = currentOffset - anchor.offset;
      element.scrollTop += correction;
      return correction;
    };
    const correction = correctFromDom();
    historyTrace.record({ kind: "restore", key: anchor.key, correction });
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
        historyTrace.record({ kind: "settled", settled });
      }
    );
    layoutSettle.current = settle;
    element.dataset.layoutSettled = "false";
    publishMode();
    window.agentplaneHistoryTrace = () => historyTrace.events();
    window.agentplaneHistoryEstimateErrors = () => historyTrace.estimateErrors();
    return () => {
      settle.dispose();
      layoutSettle.current = null;
      delete window.agentplaneHistoryTrace;
      delete window.agentplaneHistoryEstimateErrors;
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
        historyTrace.record({ kind: "prepend", added });
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
      historyTrace.record({ kind: "resize", scrollTop: element.scrollTop, scrollHeight: element.scrollHeight, pinned });
      if (pinned) element.scrollTop = element.scrollHeight;
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
      historyTrace.record({
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
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) expectUserScroll();
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) setFollowing(false, "key-up");
      }}
      onKeyUp={() => {
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onPointerDown={() => {
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
        if (!(event.target instanceof Element) || !event.target.closest("summary, [aria-expanded]")) return;
        if (element.scrollHeight > element.clientHeight) {
          setFollowing(false, "disclosure-click");
          clickedAt.current = element.scrollTop;
        }
        captureReadingAnchor(element);
        historyTrace.record({
          kind: "click",
          scrollTop: element.scrollTop,
          scrollHeight: element.scrollHeight,
          clientHeight: element.clientHeight,
        });
      }}
      onScroll={(event) => {
        const element = event.currentTarget;
        if (element.scrollTop !== clickedAt.current) clickedAt.current = null;
        const followed = followPreviousBottom(element);
        recentBottoms.current = [element.scrollHeight - element.clientHeight];
        historyTrace.record({
          kind: "scroll",
          scrollTop: element.scrollTop,
          scrollHeight: element.scrollHeight,
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
          historyTrace.record({ kind: "load-older" });
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
                position: "absolute",
                top: 0,
                left: 0,
                width: "100%",
                transform: `translateY(${item.start}px)`,
                paddingBottom: 4,
              }}
            >
              <HistoryRowView
                threadId={threadId}
                row={row}
                live={(entity) => running && entity.turnId === activeTurn}
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
            zIndex: 1,
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

function ProjectedSessionBody({
  threadId,
  entities,
  thread,
  history,
  available,
  onStatusChange,
}: {
  threadId: string;
  entities: ThreadEntity[];
  thread: ThreadView;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
  available: boolean;
  onStatusChange: (status: ThreadTabTitleStatus) => void;
}): JSX.Element {
  const [draft, setDraft] = useState("");
  const sync = useThreadSync().useThread();
  const commands = useProjectedCommands(threadId, entities);
  const openDebug = useOpenChronologicalDebug();
  const view = entities.find((row) => row.entityKind === "view_state");
  const controls = view && "controls" in view.state ? view.state.controls : null;
  const operational = view && "controls" in view.state ? view.state.operational : null;
  const running =
    available && !thread.archived && operational?.status === "active" && controls?.harness_state === "running";
  const canResume =
    available &&
    !thread.archived &&
    !["SETUP_STATE_RUNNING", "SETUP_STATE_FAILED", "SETUP_STATE_INTERRUPTED"].includes(thread.setup_state ?? "") &&
    operational?.status !== "failed" &&
    (operational?.status !== "active" || controls?.harness_state !== "running");
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const activeTurn = controls?.active_turn_id ?? null;
  const threadsLive = useRequiredThreadsLive();
  const status = threadStatusFromSnapshot(
    threadsLive.snapshot?.threads.find((candidate) => candidate.id === threadId),
    threadsLive.snapshot?.sandboxes.find((candidate) => candidate.name === thread.sandbox),
    snapshotFresh(threadsLive)
  );
  useEffect(
    () => onStatusChange({ kind: status.kind, tabLabel: status.tabLabel }),
    [onStatusChange, status.kind, status.tabLabel]
  );
  useEffect(() => installThreadFavicon(status.kind), [status.kind]);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  const selectedModel = controls?.applied_model ?? thread.model;
  const effortOptions = modelOptions.find((option) => option.model === selectedModel)?.reasoning_efforts ?? [];
  useEffect(() => {
    let active = true;
    void models().then(
      (catalog) => {
        if (active) setModelOptions(modelsForHarness(catalog, thread.harness));
      },
      (reason: unknown) => {
        if (active) setModelError(displayableError(reason));
      }
    );
    return () => {
      active = false;
    };
  }, [thread.harness]);
  const rows = historyRows(
    entities
      .filter((row) => ["item", "confirmed_input", "lifecycle"].includes(row.entityKind) || pendingSentMessage(row))
      .sort((left, right) =>
        decimalBigInt(left.cursor) < decimalBigInt(right.cursor)
          ? -1
          : decimalBigInt(left.cursor) > decimalBigInt(right.cursor)
            ? 1
            : 0
      )
  );
  const selectedCommandIds = commands.local.commands.slice(0, 128);
  const pendingInputMessages = (
    <PendingInputMessages
      commands={selectedCommandIds}
      entities={entities}
      errors={commands.errors}
      store={commands.store}
      deliver={commands.deliver}
    />
  );

  // Two Enters before the cleared draft renders would otherwise submit the same text twice, under
  // two command ids. Guards one render, not the lifetime of any HTTP request or command.
  const submitting = useRef(false);
  useEffect(() => {
    submitting.current = false;
  }, [draft]);

  function submit(): void {
    if (!draft.trim() || !running || submitting.current) return;
    submitting.current = true;
    const value = create(CommandSchema, {
      commandId: crypto.randomUUID(),
      operation: { case: "submitInput", value: { text: draft } },
    });
    if (commands.submit(value)) setDraft("");
    else submitting.current = false;
  }

  async function resume(): Promise<void> {
    if (!canResume || resuming) return;
    setResuming(true);
    setResumeError(null);
    try {
      await resumeThread(threadId);
    } catch (reason: unknown) {
      setResumeError(displayableError(reason));
    } finally {
      setResuming(false);
    }
  }

  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key !== "Enter") return;
    event.preventDefault();
    if (!(event.ctrlKey || event.metaKey || event.shiftKey)) {
      submit();
      return;
    }
    // Insert the newline by hand: the preventDefault above already swallowed whatever the browser
    // would otherwise have done for Ctrl/Cmd/Shift+Enter, and setting a controlled value leaves the
    // caret at the end, so put it back where the newline went.
    const field = event.currentTarget;
    const at = field.selectionStart;
    setDraft(`${draft.slice(0, at)}\n${draft.slice(field.selectionEnd)}`);
    requestAnimationFrame(() => field.setSelectionRange(at + 1, at + 1));
  }

  return (
    <RetainedDisclosureProvider>
      <Stack
        style={{ flex: 1, minHeight: 0 }}
        data-projection-cursor={view ? decimalBigInt(view.revisionCursor).toString() : undefined}
      >
        <VirtualizedHistory
          threadId={threadId}
          rows={rows}
          tail={pendingInputMessages}
          running={running}
          activeTurn={activeTurn}
          history={history}
        />
        <ProjectedCommandRows
          threadId={threadId}
          entities={entities}
          localCommands={commands.local.commands}
          dismissedCommandIds={commands.local.dismissedCommandIds}
        />
        {selectedCommandIds.length > 0 && (
          <SelectedCommandOutcomes
            commands={selectedCommandIds}
            store={commands.store}
            errors={commands.errors}
            deliver={commands.deliver}
          />
        )}
        {operational?.feed_error && (
          <Text role="alert" c="red">
            {operational.feed_error.cursor === null
              ? `Projection failed: ${operational.feed_error.message}. Showing verified history through event ${operational.last_verified_cursor}.`
              : `Rejected event ${operational.feed_error.cursor}: ${operational.feed_error.message}. Showing verified history through event ${operational.last_verified_cursor}.`}
          </Text>
        )}
        {modelError && (
          <Text role="alert" c="red">
            {modelError}
          </Text>
        )}
        {commands.submissionError && (
          <Text role="alert" c="red">
            {commands.submissionError}
          </Text>
        )}
        {resumeError && (
          <Text role="alert" c="red">
            Could not resume harness: {resumeError}
          </Text>
        )}
        <ComposerPendingActions />
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          placeholder="Enter sends, Shift+Enter or Ctrl+Enter for a new line"
          autosize
          minRows={2}
          maxRows={12}
          disabled={!running}
          onKeyDown={composerKey}
        />
        <Group className="agentplane-composer-controls" justify="space-between" gap="xs" wrap="nowrap" pb="xs">
          <Group className="agentplane-composer-settings" gap="xs" wrap="nowrap">
            {canResume && (
              <Button size="xs" aria-label="Resume harness" loading={resuming} onClick={() => void resume()}>
                Resume harness
              </Button>
            )}
            <Select
              aria-label="Model"
              className="agentplane-composer-model"
              data={modelOptions.map((option) => ({ value: option.model, label: option.display_name }))}
              value={controls?.applied_model ?? null}
              placeholder={
                sync.window?.error || operational?.status === "failed"
                  ? "Model unavailable"
                  : !sync.window?.caughtUp
                    ? "Catching up…"
                    : "Model"
              }
              disabled={!running}
              onChange={(model) =>
                model &&
                commands.submit(
                  create(CommandSchema, {
                    commandId: crypto.randomUUID(),
                    operation: { case: "changeModel", value: { model } },
                  })
                )
              }
            />
            {effortOptions.length > 0 && (
              <Select
                aria-label="Reasoning effort"
                className="agentplane-composer-effort"
                data={effortOptions}
                value={controls?.applied_reasoning_effort ?? thread.reasoning_effort ?? null}
                placeholder="Effort"
                disabled={!running}
                onChange={(effort) =>
                  effort &&
                  commands.submit(
                    create(CommandSchema, {
                      commandId: crypto.randomUUID(),
                      operation: { case: "changeReasoningEffort", value: { effort } },
                    })
                  )
                }
              />
            )}
          </Group>
          <TopbarActions>
            <Menu position="bottom-end" withArrow shadow="md">
              <Menu.Target>
                <ActionIcon size="sm" variant="subtle" color="gray" aria-label="More">
                  <IconDotsVertical size={16} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label style={{ overflowWrap: "anywhere" }}>Thread ID: {threadId}</Menu.Label>
                <Menu.Divider />
                <Menu.Item leftSection={<IconHistory size={15} />} onClick={() => openDebug()}>
                  Debug history
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  color="red"
                  leftSection={<IconPower size={15} />}
                  disabled={!running}
                  onClick={() =>
                    commands.submit(
                      create(CommandSchema, {
                        commandId: crypto.randomUUID(),
                        operation: { case: "stopRunnerSession", value: {} },
                      })
                    )
                  }
                >
                  Shut down harness
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </TopbarActions>
          <Group className="agentplane-composer-send" gap="xs" wrap="nowrap">
            <ActionIcon
              size="lg"
              variant="light"
              color="red"
              aria-label="Interrupt"
              disabled={!running || !activeTurn}
              onClick={() =>
                activeTurn &&
                commands.submit(
                  create(CommandSchema, {
                    commandId: crypto.randomUUID(),
                    operation: { case: "interruptTurn", value: { turnId: activeTurn } },
                  })
                )
              }
            >
              <IconPlayerStop size={16} />
            </ActionIcon>
            <ActionIcon size="lg" aria-label="Send" disabled={!running || !draft.trim()} onClick={submit}>
              <IconSend size={16} />
            </ActionIcon>
          </Group>
        </Group>
      </Stack>
    </RetainedDisclosureProvider>
  );
}

function SyncedThread({
  threadId,
  thread,
  available,
  inventory,
  onStatusChange,
}: {
  threadId: string;
  thread: ThreadView;
  available: boolean;
  /** The sandbox inventory's stream, which the page's one stale notice covers too. */
  inventory: StreamStatus;
  onStatusChange: (status: ThreadTabTitleStatus) => void;
}): JSX.Element {
  const { window: shown, error } = useThreadSync().useThread();
  // A stopped window is not following the thread at all, and its alert says so.
  const stream = useOptionalStreamStatus("Thread", shown !== null && shown.error === null ? shown.connection : null);
  if (!shown) {
    if (error) return <p role="alert">Thread sync failed: {error}</p>;
    return <p role="status">Loading thread…</p>;
  }
  return (
    <>
      <StaleNotice streams={[inventory, stream]} />
      {error && <p role="alert">Thread sync failed: {error}; showing the current window and retrying.</p>}
      {shown.error && (
        <p role="alert">
          Thread synchronization stopped: {shown.error} <button onClick={shown.refresh}>Refresh thread</button>
        </p>
      )}
      {!shown.error && !shown.caughtUp && (
        <p role="status" data-thread-catchup="true">
          Catching up thread…
        </p>
      )}
      <ProjectedSessionBody
        threadId={threadId}
        entities={shown.caughtUp ? shown.rows : []}
        thread={thread}
        history={shown}
        available={available}
        onStatusChange={onStatusChange}
      />
    </>
  );
}

/** Why the thread's sandbox cannot take commands, as the inventory last reported it; the composer's
 * dot says only "Sandbox unavailable". An absence reads as a deletion only from a current inventory:
 * a stale or dropped stream may not have seen the sandbox since. */
function sandboxNotice(sandbox: SandboxView | undefined, inventoryFresh: boolean): string | null {
  if (sandbox === undefined) {
    return inventoryFresh
      ? "Sandbox no longer exists. Showing archived Thread history; controls are disabled."
      : "Sandbox absent from last inventory snapshot. Current availability unknown; controls are disabled.";
  }
  if (sandboxReady(sandbox)) return null;
  return `Last observed Sandbox and Pod: ${sandboxSummary(sandbox).label}. Showing retained Thread history; controls are disabled.`;
}

function harnessLabel(harness: ThreadView["harness"]): string {
  switch (harness) {
    case "HARNESS_CLAUDE":
      return "Claude";
    case "HARNESS_CODEX":
      return "Codex";
    default:
      return "Unknown harness";
  }
}

export function ProjectedSession({
  threadId,
  settingsOpen = false,
}: {
  threadId: string;
  settingsOpen?: boolean;
}): JSX.Element {
  const sync = useThreadSync();
  const [thread, setThread] = useState<ThreadView | null>(null);
  const [tabStatus, setTabStatus] = useState(CONNECTING_TAB_STATUS);
  const threadsLive = useRequiredThreadsLive();
  const topbarStatus = threadStatusFromSnapshot(
    threadsLive.snapshot?.threads.find((candidate) => candidate.id === threadId),
    threadsLive.snapshot?.sandboxes.find((candidate) => candidate.name === thread?.sandbox),
    snapshotFresh(threadsLive)
  );
  const [error, setError] = useState<string | null>(null);
  const environment = useLive<SandboxesSnapshot>(liveSandboxesUrl(), "Sandboxes");
  const inventoryFresh = environment.stream.standing === "current" && environment.health?.fresh === true;
  const sandbox = environment.snapshot?.sandboxes.find((candidate) => candidate.name === thread?.sandbox);
  const notice = thread && environment.snapshot && sandboxNotice(sandbox, inventoryFresh);
  useEffect(() => {
    void getThread(threadId).then(setThread, (reason: unknown) => setError(displayableError(reason)));
  }, [threadId]);
  useEffect(() => {
    document.title = settingsOpen
      ? appDocumentTitle("/", true)
      : threadDocumentTitle(thread?.name, threadId, tabStatus);
  }, [thread?.name, tabStatus, threadId, settingsOpen]);
  return (
    <ChronologicalDebugProvider key={threadId} threadId={threadId}>
      <TopbarTitle>
        <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
          {thread && <ThreadStatusIndicator kind={topbarStatus.kind} label={topbarStatus.label} />}
          <Box style={{ flex: 1, minWidth: 0 }}>
            <ThreadTitle threadId={threadId} thread={thread} onRenamed={setThread} onError={setError} />
          </Box>
          {thread && (
            <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
              {thread.sandbox} · {harnessLabel(thread.harness)}
            </Text>
          )}
        </Group>
      </TopbarTitle>
      <Stack style={{ flex: 1, minHeight: 0 }}>
        {/* The controls wait on this stream's word that the sandbox runs, so one down past a blip, or
            whose watch has stalled, disables them as surely as a stopped sandbox. The sidebar's
            connection indicator says the first; this says the second. */}
        <LiveStatus live={environment} />
        {!thread && <StaleNotice streams={[environment.stream]} />}
        {error && (
          <Text role="alert" c="red">
            {error}
          </Text>
        )}
        {thread?.archived && (
          <Text role="status" c="dimmed">
            Thread archived. Showing retained thread history; controls are disabled.
          </Text>
        )}
        {notice && (
          <Text role="status" c="dimmed">
            {notice}
          </Text>
        )}
        {thread && (
          <sync.Thread key={threadId} threadId={threadId}>
            <SyncedThread
              threadId={threadId}
              thread={thread}
              available={inventoryFresh && sandboxReady(sandbox)}
              inventory={environment.stream}
              onStatusChange={setTabStatus}
            />
          </sync.Thread>
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
