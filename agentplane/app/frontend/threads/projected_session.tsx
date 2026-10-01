import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Flex,
  Group,
  Menu,
  Paper,
  Select,
  Stack,
  Text,
  Textarea,
} from "@mantine/core";
import { create } from "@bufbuild/protobuf";
import { useVirtualizer } from "@tanstack/react-virtual";
import IconDotsVertical from "@tabler/icons-react/dist/esm/icons/IconDotsVertical.mjs";
import IconHistory from "@tabler/icons-react/dist/esm/icons/IconHistory.mjs";
import IconPlayerStop from "@tabler/icons-react/dist/esm/icons/IconPlayerStop.mjs";
import IconPower from "@tabler/icons-react/dist/esm/icons/IconPower.mjs";
import IconSend from "@tabler/icons-react/dist/esm/icons/IconSend.mjs";
import { type JSX, type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";

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
import { decimalBigInt, useThreadSync, type ThreadEntity, type ThreadState, type ThreadWindow } from "./thread_sync";
import { historyRows, rowKey, summarizeLifecycleGroup, summarizeRun, type HistoryRow } from "./history_rows";
import { liveSandboxesUrl, LiveStatus, useLive, useRequiredThreadsLive, type SandboxesSnapshot } from "../live";
import { StaleNotice, useStreamStatus, type StreamStatus } from "../stream_status";
import { RetainedDisclosure, RetainedDisclosureProvider, useRetainedDisclosure } from "./retained_disclosures";
import { CollapsibleCard, EntityCard, ItemStatus, pendingSentMessage } from "./thread_cards";
import { ProjectedCommandRows, SelectedCommandOutcomes, useProjectedCommands } from "./thread_commands";
import { ChronologicalDebugProvider, useOpenChronologicalDebug } from "./chronological_debug";
import { ThreadTitle } from "./thread_title";
import { ThreadStatusDot } from "../thread_status_dot";
import { snapshotFresh, threadStatusFromSnapshot } from "../thread_status";
import { TopbarActions, TopbarTitle } from "../topbar";
import { installThreadFavicon, type ThreadFaviconPulseEpoch } from "../thread_favicon";
import { appDocumentTitle, threadDocumentTitle } from "../tab_metadata";
import "./projected_session.css";

/** A run of tool calls and reasoning steps, folded behind its summary until opened. */
function CollapsibleRows({
  id,
  summary,
  threadId,
  entities,
  live,
}: {
  id: string;
  summary: ReactNode;
  threadId: string;
  entities: ThreadEntity[];
  live: (entity: ThreadEntity) => boolean;
}): JSX.Element {
  const [open] = useRetainedDisclosure(id);
  return (
    <CollapsibleCard open={open}>
      <RetainedDisclosure id={id} summary={summary}>
        <Stack gap="xs" mt="xs">
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

// Traces VirtualizedHistory's scroll-anchor bookkeeping to the console: off by default (this ran
// hot enough, once, to matter) -- flip on with localStorage.setItem("agentplane:debugScroll", "1")
// when chasing a reader-position bug, then reload.
const SCROLL_DEBUG = typeof window !== "undefined" && window.localStorage?.getItem("agentplane:debugScroll") === "1";
function scrollDebug(...args: unknown[]): void {
  if (SCROLL_DEBUG) console.debug("[scroll-anchor]", ...args);
}

function VirtualizedHistory({
  threadId,
  rows,
  running,
  activeTurn,
  history,
}: {
  threadId: string;
  rows: HistoryRow[];
  running: boolean;
  activeTurn: string | null;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
}): JSX.Element {
  const viewport = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const previousScrollTop = useRef(0);
  // Every bottom the viewport has had since the last scroll event or content resize was handled.
  // A return to the bottom lands on whichever one was current when it ran; a card can grow in
  // that task or an earlier one before the browser dispatches the scroll event.
  const recentBottoms = useRef<number[]>([]);
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
  const cancelRestoration = () => {
    if (restorationFrame.current !== null) cancelAnimationFrame(restorationFrame.current);
    restorationFrame.current = null;
    restorationSize.current = null;
    restoringAnchor.current = null;
  };
  const recordBottom = (element: HTMLDivElement) => {
    const bottom = element.scrollHeight - element.clientHeight;
    if (!recentBottoms.current.includes(bottom)) recentBottoms.current.push(bottom);
  };
  // Restoring to a row that is not mounted scrolls to its estimated offset. When the rows above it
  // measure shorter than estimated, that offset lies past the end and the browser clamps it: the
  // restoration's own scroll lands on the bottom, which is not the reader returning there.
  const restoringScroll = () => restorationFrame.current !== null;
  const followPreviousBottom = (element: HTMLDivElement) => {
    // A programmatic return to the old bottom can be delivered after a card grows. Preserve
    // it before restoring a stale reader anchor, while an explicit user gesture owns its scroll,
    // as does a restoration still settling.
    if (captureNextScroll.current || restoringScroll()) return false;
    if (!recentBottoms.current.some((bottom) => Math.abs(element.scrollTop - bottom) <= 2)) return false;
    atBottom.current = true;
    cancelRestoration();
    element.scrollTop = element.scrollHeight;
    return true;
  };
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
    estimateSize: () => 180,
    getItemKey: (index) => rowKey(rows[index]),
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 5 + pageOverscan,
    onChange: (instance, sync) => {
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
  const loadOlderAtTop = () => {
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
  };
  const captureReadingAnchor = (element: HTMLDivElement) => {
    const viewportTop = element.getBoundingClientRect().top;
    const first = [...element.querySelectorAll<HTMLElement>("[data-thread-anchor]")].find(
      (candidate) => candidate.getBoundingClientRect().bottom > viewportTop
    );
    const firstRow = first
      ? rows.find((row) => row.entities[0].cursor.toString() === first.dataset.threadAnchor)
      : undefined;
    if (first && firstRow) {
      readingAnchor.current = { key: rowKey(firstRow), offset: first.getBoundingClientRect().top - viewportTop };
      scrollDebug("captureReadingAnchor", readingAnchor.current);
    }
  };
  const restoreAnchor = (anchor: { key: string; offset: number }, awaitMeasurement = false) => {
    const index = anchorIndex(anchor.key);
    scrollDebug("restoreAnchor called", anchor, "index", index, "awaitMeasurement", awaitMeasurement);
    if (index < 0) return;
    cancelRestoration();
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
    scrollDebug("restoreAnchor correction", correction);
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
      });
    });
  };
  useLayoutEffect(() => {
    const element = viewport.current;
    const firstKey = rows[0] ? rowKey(rows[0]) : null;
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
        scrollDebug("page prepended, widening overscan by", added);
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
      // A scrollbar drag or programmatic equivalent can reach the old bottom in the same task
      // that grows the last card, before the browser dispatches its scroll event. Preserve that
      // user choice across the resize without interpreting arbitrary layout movement as intent.
      if (followPreviousBottom(element) || atBottom.current) element.scrollTop = element.scrollHeight;
      // Content can resize while a wheel, touch, or key scroll is still settling. Its
      // measured rows do not describe the reader's final position yet; scrollend will
      // capture that position before a later resize restoration is eligible.
      else if (!captureNextScroll.current && readingAnchor.current) restoreAnchor(readingAnchor.current, true);
      recentBottoms.current = [element.scrollHeight - element.clientHeight];
    });
    observer.observe(content);
    // A body arriving for a remounted or streaming card is a DOM mutation, and the scroll event
    // of a return to the bottom that follows it in the same frame precedes the ResizeObserver
    // delivery for it. The mutation callback runs before any later task, so record its bottom.
    const mutations = new MutationObserver(() => recordBottom(element));
    mutations.observe(content, { subtree: true, childList: true, characterData: true, attributes: true });
    return () => {
      observer.disconnect();
      mutations.disconnect();
    };
  }, [rows, virtualizer]);
  // The observers above re-subscribe on every render's rows; a restoration in flight outlives that.
  useLayoutEffect(() => cancelRestoration, []);
  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const onScrollEnd = () => {
      scrollDebug(
        "onScrollEnd",
        "restoringAnchor",
        restoringAnchor.current,
        "captureNextScroll",
        captureNextScroll.current
      );
      if (restoringAnchor.current !== null || !captureNextScroll.current) return;
      captureReadingAnchor(element);
      captureNextScroll.current = false;
      loadOlderAtTop();
    };
    element.addEventListener("scrollend", onScrollEnd);
    return () => element.removeEventListener("scrollend", onScrollEnd);
  }, [rows]);
  useEffect(loadOlderAtTop);
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
          if (event.deltaY < 0) atBottom.current = false;
        } else if (!scrolledSinceInput.current) {
          captureNextScroll.current = false;
        }
      }}
      onKeyDown={(event) => {
        cancelRestoration();
        if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) expectUserScroll();
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) atBottom.current = false;
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
        if (next !== undefined && touchY.current !== null && next > touchY.current) atBottom.current = false;
        touchY.current = next ?? null;
      }}
      onTouchEnd={() => {
        touchY.current = null;
        if (!scrolledSinceInput.current) captureNextScroll.current = false;
      }}
      onScroll={(event) => {
        const element = event.currentTarget;
        const followed = followPreviousBottom(element);
        recentBottoms.current = [element.scrollHeight - element.clientHeight];
        scrollDebug(
          "onScroll",
          "scrollTop",
          element.scrollTop,
          "followed",
          followed,
          "restoringAnchor",
          restoringAnchor.current,
          "captureNextScroll",
          captureNextScroll.current
        );
        if (followed) {
          previousScrollTop.current = element.scrollTop;
          return;
        }
        const movedUp = element.scrollTop < previousScrollTop.current;
        const movedDown = element.scrollTop > previousScrollTop.current;
        const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        if (movedUp) {
          atBottom.current = false;
        } else if (!restoringScroll() && nearBottom && (atBottom.current || movedDown)) {
          atBottom.current = true;
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
          scrollDebug("calling loadOlder from onScroll");
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
    </div>
  );
}

type Operational = Extract<ThreadEntity["state"], { operational: unknown }>["operational"];

function ProjectedSessionBody({
  threadId,
  entities,
  thread,
  history,
  available,
  onStatusLabelChange,
}: {
  threadId: string;
  entities: ThreadEntity[];
  thread: ThreadView;
  history: Pick<ThreadWindow, "olderAvailable" | "loadingOlder" | "loadOlder">;
  available: boolean;
  onStatusLabelChange: (label: string) => void;
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
  const pulseEpoch = useRef<ThreadFaviconPulseEpoch>({ current: null });
  if (status.pulse) pulseEpoch.current.current ??= Date.now();
  else pulseEpoch.current.current = null;
  useEffect(() => onStatusLabelChange(status.tabLabel), [onStatusLabelChange, status.tabLabel]);
  useEffect(() => installThreadFavicon(status, pulseEpoch.current), [status.color, status.pulse]);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
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
          running={running}
          activeTurn={activeTurn}
          history={history}
        />
        <ProjectedCommandRows threadId={threadId} entities={entities} localCommands={commands.local.commands} />
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
        <Group justify="space-between" wrap="nowrap" pb="xs">
          <Group gap="xs" wrap="nowrap">
            <ThreadStatusDot color={status.color} label={status.label} pulse={status.pulse} />
            {canResume && (
              <Button size="xs" aria-label="Resume harness" loading={resuming} onClick={() => void resume()}>
                Resume harness
              </Button>
            )}
            <Select
              aria-label="Model"
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
              w={200}
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
          <Group gap="xs" wrap="nowrap">
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
  onStatusLabelChange,
}: {
  threadId: string;
  thread: ThreadView;
  available: boolean;
  /** The sandbox inventory's stream, which the page's one stale notice covers too. */
  inventory: StreamStatus;
  onStatusLabelChange: (label: string) => void;
}): JSX.Element {
  const { window: shown, error } = useThreadSync().useThread();
  // A stopped window is not following the thread at all, and its alert says so.
  const stream = useStreamStatus("Thread", shown !== null && shown.error === null ? shown.connection : null);
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
        onStatusLabelChange={onStatusLabelChange}
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
  if (sandbox.state === "running") return null;
  return `Last observed Sandbox state: ${sandbox.state}. Showing retained Thread history; controls are disabled.`;
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
  const [tabStatus, setTabStatus] = useState("Connecting");
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
              available={inventoryFresh && sandbox?.state === "running"}
              inventory={environment.stream}
              onStatusLabelChange={setTabStatus}
            />
          </sync.Thread>
        )}
      </Stack>
    </ChronologicalDebugProvider>
  );
}
