import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import type { SessionSummary } from "./schema";
import { ApiError, getSession, listSessions, watchSessions } from "./api";
import { errorMessage, retryTransient } from "./session-fetch";
import { mergeSessionPage, sessionMatchesFilter, sortSessions, type StatusFilter } from "./session-info";

const ALL_STATUSES = ["active", "paused", "archived"];
type WatchStatus = "connecting" | "connected" | "reconnecting";

type SessionDirectory = {
  sessions: SessionSummary[];
  loadingSessions: boolean;
  nextSessionCursor: string | null;
  loadingMoreSessions: boolean;
  sessionError: string | null;
  resumeToken: string | null;
  watchStatus: WatchStatus;
  loadMoreSessions: () => Promise<void>;
};

export function useSessionDirectory({
  filter,
  selectedId,
  setSelectedId,
  refreshSelectedEvents,
}: {
  filter: StatusFilter;
  selectedId: string | null;
  setSelectedId: Dispatch<SetStateAction<string | null>>;
  refreshSelectedEvents: (sessionId: string | null) => void;
}): SessionDirectory {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const [nextSessionCursor, setNextSessionCursor] = useState<string | null>(null);
  const [resumeToken, setResumeToken] = useState<string | null>(null);
  const [watchStatus, setWatchStatus] = useState<WatchStatus>("connecting");
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const filterRef = useRef(filter);
  filterRef.current = filter;
  const sessionsFilter = useRef<StatusFilter | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(true);
  const [loadingMoreSessions, setLoadingMoreSessions] = useState(false);
  const [sessionReload, setSessionReload] = useState(0);
  const sessionVersions = useRef(new Map<string, number>());
  const appliedDetailVersions = useRef(new Map<string, number>());
  const detailRequests = useRef(new Map<string, AbortController>());
  const olderSessionsRequest = useRef<AbortController | null>(null);

  const replaceSessions = useCallback((next: SessionSummary[]): void => {
    sessionsRef.current = next;
    setSessions(next);
  }, []);

  const applySessionDetail = useCallback(
    (sessionId: string, summary: SessionSummary | null, appliedVersion: number): void => {
      const previous = sessionsRef.current;
      const retained = previous.filter((candidate) => candidate.id !== sessionId);
      appliedDetailVersions.current.set(sessionId, appliedVersion);
      const next = sortSessions(
        summary !== null && sessionMatchesFilter(summary, filterRef.current) ? [summary, ...retained] : retained
      );
      replaceSessions(next);
      if (selectedIdRef.current === sessionId && !next.some((candidate) => candidate.id === sessionId)) {
        setSelectedId(next[0]?.id ?? null);
      }
    },
    [replaceSessions, setSelectedId]
  );

  const loadSessionDetail = useCallback(
    async (sessionId: string, controller: AbortController): Promise<void> => {
      while (!controller.signal.aborted) {
        const requestedVersion = sessionVersions.current.get(sessionId) ?? 0;
        try {
          const response = await retryTransient(() => getSession(sessionId, controller.signal), controller.signal);
          if (controller.signal.aborted) return;
          applySessionDetail(sessionId, response.session, requestedVersion);
          if (requestedVersion !== (sessionVersions.current.get(sessionId) ?? 0)) continue;
          return;
        } catch (reason) {
          if (controller.signal.aborted) return;
          if (requestedVersion !== (sessionVersions.current.get(sessionId) ?? 0)) continue;
          if (reason instanceof ApiError && reason.status === 404) {
            applySessionDetail(sessionId, null, requestedVersion);
          } else {
            setSessionError(errorMessage(reason));
          }
          return;
        }
      }
    },
    [applySessionDetail]
  );

  const refreshSessionDetail = useCallback(
    (sessionId: string): void => {
      sessionVersions.current.set(sessionId, (sessionVersions.current.get(sessionId) ?? 0) + 1);
      if (detailRequests.current.has(sessionId)) return;
      const controller = new AbortController();
      detailRequests.current.set(sessionId, controller);
      void loadSessionDetail(sessionId, controller).finally(() => {
        if (detailRequests.current.get(sessionId) === controller) detailRequests.current.delete(sessionId);
      });
    },
    [loadSessionDetail]
  );

  const handleChangedSessions = useCallback(
    (sessionIds: string[]): void => {
      const changedIds = [...new Set(sessionIds)];
      for (const sessionId of changedIds) refreshSessionDetail(sessionId);
      const selected = selectedIdRef.current;
      if (selected !== null && changedIds.includes(selected)) refreshSelectedEvents(selected);
    },
    [refreshSelectedEvents, refreshSessionDetail]
  );

  const handleWatchReset = useCallback((): void => {
    for (const session of sessionsRef.current) refreshSessionDetail(session.id);
    setSessionReload((count) => count + 1);
    refreshSelectedEvents(selectedIdRef.current);
  }, [refreshSelectedEvents, refreshSessionDetail]);

  const changedHandlerRef = useRef(handleChangedSessions);
  changedHandlerRef.current = handleChangedSessions;
  const resetHandlerRef = useRef(handleWatchReset);
  resetHandlerRef.current = handleWatchReset;

  useEffect(() => {
    let current = true;
    const controller = new AbortController();
    const filterChanged = sessionsFilter.current !== filter;
    const statuses = filter === "all" ? ALL_STATUSES : [filter];
    const versionsAtStart = new Map(sessionVersions.current);
    const appliedVersionsAtStart = new Map(appliedDetailVersions.current);
    olderSessionsRequest.current?.abort();
    olderSessionsRequest.current = null;
    setLoadingMoreSessions(false);
    setSessionError(null);
    void retryTransient(() => listSessions(statuses, undefined, controller.signal), controller.signal)
      .then((page) => {
        if (!current || controller.signal.aborted) return;
        sessionsFilter.current = filter;
        const previous = sessionsRef.current.filter((session) => sessionMatchesFilter(session, filter));
        const previousById = new Map(previous.map((session) => [session.id, session]));
        const currentPage = page.data.flatMap((session) => {
          const changedSinceRead =
            (sessionVersions.current.get(session.id) ?? 0) > (versionsAtStart.get(session.id) ?? 0);
          const resolved = changedSinceRead ? previousById.get(session.id) : session;
          return resolved !== undefined && sessionMatchesFilter(resolved, filter) ? [resolved] : [];
        });
        const resolvedDuringRequest = sessionsRef.current.filter(
          (session) =>
            sessionMatchesFilter(session, filter) &&
            (appliedDetailVersions.current.get(session.id) ?? 0) > (appliedVersionsAtStart.get(session.id) ?? 0)
        );
        const next = sortSessions(
          mergeSessionPage(resolvedDuringRequest, filterChanged ? currentPage : mergeSessionPage(currentPage, previous))
        );
        replaceSessions(next);
        setNextSessionCursor(page.next_cursor);
        setResumeToken((existing) => existing ?? page.resume_token ?? null);
        setSelectedId((previous) =>
          filterChanged && !next.some((session) => session.id === previous)
            ? (next[0]?.id ?? null)
            : (previous ?? next[0]?.id ?? null)
        );
        setSessionError(null);
      })
      .catch((reason: unknown) => {
        if (current && !controller.signal.aborted) setSessionError(errorMessage(reason));
      })
      .finally(() => {
        if (current && !controller.signal.aborted) setLoadingSessions(false);
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [filter, sessionReload, replaceSessions, setSelectedId]);

  useEffect(() => {
    if (resumeToken === null) return;
    const watch = watchSessions(resumeToken);
    const recoverMalformedFrame = (): void => {
      setSessionError("The live update stream sent an invalid change. Reloading the loaded session list.");
      resetHandlerRef.current();
    };
    const changed = (event: Event): void => {
      if (!(event instanceof MessageEvent)) {
        recoverMalformedFrame();
        return;
      }
      try {
        const payload: unknown = JSON.parse(event.data);
        if (
          typeof payload !== "object" ||
          payload === null ||
          !Array.isArray((payload as { session_ids?: unknown }).session_ids)
        ) {
          recoverMalformedFrame();
          return;
        }
        const ids = (payload as { session_ids: unknown[] }).session_ids.filter(
          (sessionId): sessionId is string => typeof sessionId === "string" && sessionId.length > 0
        );
        if (ids.length !== (payload as { session_ids: unknown[] }).session_ids.length) {
          recoverMalformedFrame();
          return;
        }
        changedHandlerRef.current(ids);
      } catch {
        recoverMalformedFrame();
      }
    };
    const reset = (): void => resetHandlerRef.current();
    setWatchStatus("connecting");
    watch.onopen = () => setWatchStatus("connected");
    watch.onerror = () => setWatchStatus("reconnecting");
    watch.addEventListener("changed", changed);
    watch.addEventListener("reset", reset);
    return () => {
      watch.removeEventListener("changed", changed);
      watch.removeEventListener("reset", reset);
      watch.close();
    };
  }, [resumeToken]);

  useEffect(
    () => () => {
      olderSessionsRequest.current?.abort();
      for (const controller of detailRequests.current.values()) controller.abort();
      detailRequests.current.clear();
    },
    []
  );

  const loadMoreSessions = useCallback(async (): Promise<void> => {
    if (nextSessionCursor === null || loadingMoreSessions) return;
    const controller = new AbortController();
    olderSessionsRequest.current?.abort();
    olderSessionsRequest.current = controller;
    const requestedFilter = filter;
    const versionsAtStart = new Map(sessionVersions.current);
    setLoadingMoreSessions(true);
    setSessionError(null);
    try {
      const page = await retryTransient(
        () =>
          listSessions(
            requestedFilter === "all" ? ALL_STATUSES : [requestedFilter],
            nextSessionCursor,
            controller.signal
          ),
        controller.signal
      );
      if (controller.signal.aborted || filterRef.current !== requestedFilter) return;
      const previous = sessionsRef.current.filter((session) => sessionMatchesFilter(session, requestedFilter));
      const previousById = new Map(previous.map((session) => [session.id, session]));
      const currentPage = page.data.flatMap((session) => {
        const changedSinceRead =
          (sessionVersions.current.get(session.id) ?? 0) > (versionsAtStart.get(session.id) ?? 0);
        const resolved = changedSinceRead ? previousById.get(session.id) : session;
        return resolved !== undefined && sessionMatchesFilter(resolved, requestedFilter) ? [resolved] : [];
      });
      replaceSessions(sortSessions(mergeSessionPage(currentPage, previous)));
      setNextSessionCursor(page.next_cursor);
    } catch (reason) {
      if (!controller.signal.aborted) setSessionError(errorMessage(reason));
    } finally {
      if (olderSessionsRequest.current === controller) {
        olderSessionsRequest.current = null;
        setLoadingMoreSessions(false);
      }
    }
  }, [filter, loadingMoreSessions, nextSessionCursor, replaceSessions]);

  return {
    sessions,
    loadingSessions,
    nextSessionCursor,
    loadingMoreSessions,
    sessionError,
    resumeToken,
    watchStatus,
    loadMoreSessions,
  };
}
