import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { Event } from "./schema";
import { listSessionEvents } from "./api";
import { errorMessage, retryTransient } from "./session-fetch";

type TranscriptScrollIntent =
  | { kind: "tail"; sessionId: string }
  | {
      kind: "anchor";
      sessionId: string;
      element: HTMLElement | null;
      sequences: string[];
      top: number;
      scrollTop: number;
      scrollHeight: number;
    };

type TranscriptHistory = {
  events: Event[];
  eventError: string | null;
  loadingEvents: boolean;
  nextEventCursor: string | null;
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  setEventViewport: (viewport: HTMLDivElement | null) => void;
  loadMoreEvents: () => Promise<void>;
  refreshSelectedEvents: (sessionId: string | null) => void;
};

function sequenceOrder(left: Event, right: Event): number {
  const leftSequence = BigInt(left.sequence_num);
  const rightSequence = BigInt(right.sequence_num);
  return leftSequence < rightSequence ? -1 : leftSequence > rightSequence ? 1 : 0;
}

function mergeSessionEvents(previous: Event[], incoming: Event[]): Event[] {
  if (incoming.length === 0) return previous;
  const bySequence = new Map(previous.map((event) => [event.sequence_num, event]));
  let changed = false;
  for (const event of incoming) {
    const existing = bySequence.get(event.sequence_num);
    if (existing === undefined || JSON.stringify(existing) !== JSON.stringify(event)) {
      bySequence.set(event.sequence_num, event);
      changed = true;
    }
  }
  return changed ? [...bySequence.values()].sort(sequenceOrder) : previous;
}

export function useTranscriptHistory(selectedId: string | null): TranscriptHistory {
  const [events, setEvents] = useState<Event[]>([]);
  const loadedSession = useRef<string | null>(null);
  const loadedEventsSession = useRef<string | null>(null);
  const eventHistory = useRef<Event[]>([]);
  const eventViewport = useRef<HTMLDivElement | null>(null);
  const followTail = useRef(true);
  const pendingTranscriptScroll = useRef<TranscriptScrollIntent | null>(null);
  const [nextEventCursor, setNextEventCursor] = useState<string | null>(null);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [eventError, setEventError] = useState<string | null>(null);
  const [loadingEvents, setLoadingEvents] = useState(false);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const pendingEventRefresh = useRef<string | null>(null);
  const olderPageRequest = useRef<AbortController | null>(null);
  const eventCatchupJob = useRef<{
    sessionId: string;
    controller: AbortController;
    running: boolean;
    dirty: boolean;
  } | null>(null);
  const requestEventCatchupRef = useRef<(sessionId: string) => void>(() => {});

  const refreshSelectedEvents = useCallback((sessionId: string | null): void => {
    if (sessionId === null) return;
    if (loadedEventsSession.current === sessionId) {
      requestEventCatchupRef.current(sessionId);
    } else {
      pendingEventRefresh.current = sessionId;
    }
  }, []);

  const captureScrollAnchor = useCallback((sessionId: string): void => {
    const viewport = eventViewport.current;
    if (viewport === null) return;
    const viewportTop = viewport.getBoundingClientRect().top;
    const candidates = [...viewport.querySelectorAll<HTMLElement>("[data-fold-kind], [data-raw-event]")];
    const element = candidates.find((candidate) => candidate.getBoundingClientRect().bottom > viewportTop) ?? null;
    const sequences =
      element?.dataset.historySequences?.split(" ") ??
      (element?.dataset.sequence === undefined ? [] : [element.dataset.sequence]);
    pendingTranscriptScroll.current = {
      kind: "anchor",
      sessionId,
      element,
      sequences,
      top: element?.getBoundingClientRect().top ?? viewportTop,
      scrollTop: viewport.scrollTop,
      scrollHeight: viewport.scrollHeight,
    };
  }, []);

  const requestEventCatchup = useCallback(
    (sessionId: string): void => {
      if (loadedEventsSession.current !== sessionId || loadedSession.current !== sessionId) {
        pendingEventRefresh.current = sessionId;
        return;
      }
      let job = eventCatchupJob.current;
      if (job !== null && job.sessionId !== sessionId) {
        job.controller.abort();
        eventCatchupJob.current = null;
        job = null;
      }
      if (job === null) {
        job = { sessionId, controller: new AbortController(), running: false, dirty: false };
        eventCatchupJob.current = job;
      }
      job.dirty = true;
      if (job.running) return;
      job.running = true;
      const activeJob = job;
      void (async () => {
        while (
          activeJob.dirty &&
          !activeJob.controller.signal.aborted &&
          loadedEventsSession.current === sessionId &&
          loadedSession.current === sessionId
        ) {
          activeJob.dirty = false;
          try {
            const newestPage = await retryTransient(
              () => listSessionEvents(sessionId, undefined, "desc", activeJob.controller.signal),
              activeJob.controller.signal
            );
            if (
              activeJob.controller.signal.aborted ||
              loadedSession.current !== sessionId ||
              loadedEventsSession.current !== sessionId
            )
              return;
            const previousNewest = eventHistory.current.at(-1);
            const catchUpEvents: Event[] = [];
            if (previousNewest !== undefined) {
              const frontier = newestPage.data.reduce(
                (latest, event) => (BigInt(event.sequence_num) > latest ? BigInt(event.sequence_num) : latest),
                BigInt(previousNewest.sequence_num)
              );
              let cursor = previousNewest.event_id;
              while (true) {
                const page = await retryTransient(
                  () => listSessionEvents(sessionId, cursor, "asc", activeJob.controller.signal),
                  activeJob.controller.signal
                );
                if (
                  activeJob.controller.signal.aborted ||
                  loadedSession.current !== sessionId ||
                  loadedEventsSession.current !== sessionId
                )
                  return;
                const previousCursor = cursor;
                if (page.data.length > 0) {
                  const throughFrontier = page.data.filter((event) => BigInt(event.sequence_num) <= frontier);
                  catchUpEvents.push(...throughFrontier);
                  if (page.last_id === null) throw new Error("The event page omitted its cursor.");
                  cursor = page.last_id;
                  if (page.data.some((event) => BigInt(event.sequence_num) > frontier)) break;
                  if (throughFrontier.some((event) => BigInt(event.sequence_num) === frontier)) break;
                }
                if (!page.has_more) break;
                if (page.data.length === 0 || page.last_id === null || page.last_id === previousCursor) {
                  throw new Error("The event history cursor did not advance.");
                }
              }
            }
            if (activeJob.controller.signal.aborted || loadedSession.current !== sessionId) return;
            const merged = mergeSessionEvents(eventHistory.current, [...newestPage.data, ...catchUpEvents]);
            if (merged !== eventHistory.current) {
              if (followTail.current) {
                pendingTranscriptScroll.current = { kind: "tail", sessionId };
              } else {
                captureScrollAnchor(sessionId);
              }
              eventHistory.current = merged;
              setEvents(merged);
            }
            if (previousNewest === undefined) {
              setNextEventCursor(newestPage.has_more ? newestPage.last_id : null);
              setHasMoreEvents(newestPage.has_more);
            }
            setEventError(null);
          } catch (reason) {
            if (!activeJob.controller.signal.aborted && loadedSession.current === sessionId) {
              setEventError(errorMessage(reason));
            }
            activeJob.dirty = false;
          }
        }
      })().finally(() => {
        activeJob.running = false;
        if (eventCatchupJob.current === activeJob && activeJob.controller.signal.aborted) {
          eventCatchupJob.current = null;
        } else if (eventCatchupJob.current === activeJob && activeJob.dirty) {
          requestEventCatchupRef.current(sessionId);
        }
      });
    },
    [captureScrollAnchor]
  );
  requestEventCatchupRef.current = requestEventCatchup;

  useEffect(() => {
    const job = eventCatchupJob.current;
    if (job !== null && job.sessionId !== selectedId) {
      job.controller.abort();
      eventCatchupJob.current = null;
    }
  }, [selectedId]);

  useEffect(
    () => () => {
      eventCatchupJob.current?.controller.abort();
      eventCatchupJob.current = null;
    },
    []
  );

  const updateTail = useCallback((): void => {
    const viewport = eventViewport.current;
    if (viewport !== null) {
      followTail.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 48;
    }
  }, []);

  const setEventViewport = useCallback(
    (viewport: HTMLDivElement | null): void => {
      const previous = eventViewport.current;
      if (previous !== null) previous.removeEventListener("scroll", updateTail);
      eventViewport.current = viewport;
      if (viewport === null) return;
      viewport.addEventListener("scroll", updateTail, { passive: true });
      updateTail();
    },
    [updateTail]
  );

  useLayoutEffect(() => {
    const viewport = eventViewport.current;
    const intent = pendingTranscriptScroll.current;
    if (viewport === null || intent === null || intent.sessionId !== selectedId) return;
    if (intent.kind === "tail") {
      viewport.scrollTop = viewport.scrollHeight;
      followTail.current = true;
      pendingTranscriptScroll.current = null;
      return;
    }

    let anchor = intent.element?.isConnected === true ? intent.element : null;
    if (anchor === null && intent.sequences.length > 0) {
      anchor =
        [...viewport.querySelectorAll<HTMLElement>("[data-history-sequences], [data-sequence]")].find((candidate) => {
          const sequences =
            candidate.dataset.historySequences?.split(" ") ??
            (candidate.dataset.sequence === undefined ? [] : [candidate.dataset.sequence]);
          return intent.sequences.some((sequence) => sequences.includes(sequence));
        }) ?? null;
    }
    if (anchor !== null) {
      viewport.scrollTop += anchor.getBoundingClientRect().top - intent.top;
    } else {
      viewport.scrollTop = intent.scrollTop + (viewport.scrollHeight - intent.scrollHeight);
    }
    followTail.current = viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop <= 48;
    pendingTranscriptScroll.current = null;
  }, [events, selectedId]);

  useEffect(() => {
    let current = true;
    if (selectedId === null) {
      loadedSession.current = null;
      loadedEventsSession.current = null;
      eventHistory.current = [];
      pendingTranscriptScroll.current = null;
      pendingEventRefresh.current = null;
      olderPageRequest.current?.abort();
      olderPageRequest.current = null;
      setLoadingEvents(false);
      setLoadingMoreEvents(false);
      setEvents([]);
      setNextEventCursor(null);
      setHasMoreEvents(false);
      return () => {
        current = false;
      };
    }
    loadedSession.current = selectedId;
    loadedEventsSession.current = null;
    eventHistory.current = [];
    pendingTranscriptScroll.current = null;
    if (pendingEventRefresh.current !== selectedId) pendingEventRefresh.current = null;
    olderPageRequest.current?.abort();
    olderPageRequest.current = null;
    followTail.current = true;
    setLoadingEvents(true);
    setLoadingMoreEvents(false);
    setEvents([]);
    setNextEventCursor(null);
    setHasMoreEvents(false);
    setEventError(null);
    const controller = new AbortController();
    void retryTransient(() => listSessionEvents(selectedId, undefined, "desc", controller.signal), controller.signal)
      .then((page) => {
        if (!current || loadedSession.current !== selectedId) return;
        const merged = mergeSessionEvents([], page.data);
        eventHistory.current = merged;
        loadedEventsSession.current = selectedId;
        if (merged.length > 0) {
          pendingTranscriptScroll.current = { kind: "tail", sessionId: selectedId };
          setEvents(merged);
        }
        setNextEventCursor(page.has_more ? page.last_id : null);
        setHasMoreEvents(page.has_more);
        if (pendingEventRefresh.current === selectedId) {
          pendingEventRefresh.current = null;
          requestEventCatchupRef.current(selectedId);
        }
      })
      .catch((reason: unknown) => {
        if (current && !controller.signal.aborted) setEventError(errorMessage(reason));
      })
      .finally(() => {
        if (current) setLoadingEvents(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [selectedId]);

  const loadMoreEvents = useCallback(async (): Promise<void> => {
    const sessionId = selectedId;
    if (sessionId === null || nextEventCursor === null || loadingMoreEvents) return;
    const controller = new AbortController();
    olderPageRequest.current?.abort();
    olderPageRequest.current = controller;
    setLoadingMoreEvents(true);
    setEventError(null);
    try {
      const page = await listSessionEvents(sessionId, nextEventCursor, "desc", controller.signal);
      if (controller.signal.aborted || loadedSession.current !== sessionId) return;
      const merged = mergeSessionEvents(eventHistory.current, page.data);
      if (merged !== eventHistory.current) {
        captureScrollAnchor(sessionId);
        eventHistory.current = merged;
        setEvents(merged);
      }
      setNextEventCursor(page.has_more ? page.last_id : null);
      setHasMoreEvents(page.has_more);
    } catch (reason) {
      if (!controller.signal.aborted && loadedSession.current === sessionId) setEventError(errorMessage(reason));
    } finally {
      if (olderPageRequest.current === controller) {
        olderPageRequest.current = null;
        if (loadedSession.current === sessionId) setLoadingMoreEvents(false);
      }
    }
  }, [captureScrollAnchor, loadingMoreEvents, nextEventCursor, selectedId]);

  return {
    events,
    eventError,
    loadingEvents,
    nextEventCursor,
    hasMoreEvents,
    loadingMoreEvents,
    setEventViewport,
    loadMoreEvents,
    refreshSelectedEvents,
  };
}
