import { useMemo } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";

import type { SessionBrowserApi } from "./api";
import type { EventRecord, Harness, Machine, SessionKind, SessionSummary, SessionVersion } from "./schema";

const SESSION_PAGE_SIZE = 50;

export type SessionViewerQueryModel = {
  machines: Machine[];
  loadingMachines: boolean;
  sessions: SessionSummary[];
  sessionCursor: string | null;
  loadingSessions: boolean;
  loadingMoreSessions: boolean;
  loadedSessionScope: boolean;
  selectedSession: SessionSummary | null;
  loadingSelectedSession: boolean;
  selectedSessionError: string | null;
  versions: SessionVersion[];
  requestedVersionUnavailable: boolean;
  selectedVersionId: string | null;
  selectedVersion: SessionVersion | null;
  loadingVersions: boolean;
  events: EventRecord[];
  eventCursor: number | null;
  loadingEvents: boolean;
  loadingEarlierEvents: boolean;
  eventScope: string;
  parentSession: SessionSummary | null;
  loadingParentSession: boolean;
  parentSessionLoadError: string | null;
  childSessions: SessionSummary[];
  childSessionCursor: string | null;
  loadingChildSessions: boolean;
  loadingMoreRelatedSessions: boolean;
  childSessionsLoadError: string | null;
  hasMoreChildSessions: boolean;
  errorText: string | null;
  loadMoreSessions: () => void;
  loadEarlierEvents: () => void;
  loadMoreRelatedSessions: () => void;
  refreshSessionList: () => void;
};

export function useSessionViewerQueries({
  api,
  sessionId,
  selectedMachineId,
  selectedHarness,
  selectedKinds,
  requestedVersionId,
}: {
  api: SessionBrowserApi;
  sessionId: string | undefined;
  selectedMachineId: string;
  selectedHarness: string;
  selectedKinds: SessionKind[];
  requestedVersionId: string | null;
}): SessionViewerQueryModel {
  const queryClient = useQueryClient();
  const machinesQuery = useQuery({
    queryKey: ["ai-session-archive", "machines"],
    queryFn: ({ signal }) => api.listMachines(signal),
  });
  const machines = machinesQuery.data ?? [];
  const sessionListQueryKey = [
    "ai-session-archive",
    "sessions",
    selectedMachineId || null,
    selectedHarness || null,
    selectedKinds,
  ] as const;
  const sessionsQuery = useInfiniteQuery({
    queryKey: sessionListQueryKey,
    queryFn: ({ pageParam, signal }) =>
      api.listSessions(
        {
          ...(selectedMachineId === "" ? {} : { machineId: selectedMachineId }),
          ...(selectedHarness === "" ? {} : { harness: selectedHarness as Harness }),
          ...(pageParam === null ? {} : { cursor: pageParam }),
          kinds: selectedKinds,
          limit: SESSION_PAGE_SIZE,
        },
        signal
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selectedKinds.length > 0,
  });
  const sessionPages = sessionsQuery.data?.pages ?? [];
  const sessions = useMemo(
    () =>
      mergeById(
        [],
        sessionPages.flatMap((page) => page.data)
      ),
    [sessionPages]
  );
  const sessionCursor = sessionPages.at(-1)?.next_cursor ?? null;
  const loadingSessions = selectedKinds.length > 0 && sessionsQuery.isLoading;
  const loadingMoreSessions = sessionsQuery.isFetchingNextPage;
  const loadedSessionScope = selectedKinds.length === 0 || sessionsQuery.data !== undefined;

  const listedSelectedSession = sessions.find((session) => session.id === sessionId) ?? null;
  const selectedSessionQuery = useQuery({
    queryKey: ["ai-session-archive", "session", sessionId ?? null],
    queryFn: ({ signal }) => {
      if (sessionId === undefined) throw new Error("A session ID is required.");
      return api.getSession(sessionId, signal);
    },
    enabled: sessionId !== undefined && listedSelectedSession === null,
  });
  const fetchedSession =
    sessionId !== undefined && selectedSessionQuery.data?.id === sessionId ? selectedSessionQuery.data : null;
  const selectedSession = listedSelectedSession ?? fetchedSession;
  const loadingSelectedSession =
    sessionId !== undefined && listedSelectedSession === null && selectedSessionQuery.isLoading;
  const selectedSessionError = selectedSessionQuery.error === null ? null : errorMessage(selectedSessionQuery.error);

  const versionsQuery = useQuery({
    queryKey: [
      "ai-session-archive",
      "versions",
      selectedSession?.id ?? null,
      selectedSession?.latest_version_id ?? null,
    ],
    queryFn: ({ signal }) => {
      if (selectedSession === null) throw new Error("A selected session is required.");
      return api.listVersions(selectedSession.id, signal);
    },
    enabled: selectedSession !== null,
  });
  const versions = versionsQuery.data ?? [];
  const hasLoadedVersions = versionsQuery.data !== undefined;
  const requestedVersionUnavailable =
    requestedVersionId !== null && hasLoadedVersions && !versions.some((version) => version.id === requestedVersionId);
  const selectedVersionId = !hasLoadedVersions
    ? null
    : requestedVersionId !== null
      ? (versions.find((version) => version.id === requestedVersionId)?.id ?? null)
      : (versions.find((version) => version.id === selectedSession?.latest_version_id)?.id ?? versions[0]?.id ?? null);
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? null;
  const loadingVersions = selectedSession !== null && versionsQuery.isLoading;

  const eventQuery = useInfiniteQuery({
    queryKey: ["ai-session-archive", "events", selectedSession?.id ?? null, selectedVersionId],
    queryFn: ({ pageParam, signal }) => {
      if (selectedSession === null || selectedVersionId === null) {
        throw new Error("A selected session version is required.");
      }
      return api.listEvents(selectedSession.id, selectedVersionId, pageParam, signal);
    },
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selectedSession !== null && selectedVersionId !== null,
  });
  const eventPages = eventQuery.data?.pages ?? [];
  const events = useMemo(
    () =>
      mergeByOrdinal(
        [],
        eventPages.flatMap((page) => page.data)
      ),
    [eventPages]
  );
  const eventCursor = eventPages.at(-1)?.next_cursor ?? null;
  const loadingEvents = eventQuery.isLoading;
  const loadingEarlierEvents = eventQuery.isFetchingNextPage;
  const eventScope = (selectedSession?.id ?? "") + "\u0000" + (selectedVersionId ?? "");

  const parentSessionId = selectedSession?.parent_session_id ?? null;
  const parentSessionQuery = useQuery({
    queryKey: ["ai-session-archive", "session", parentSessionId],
    queryFn: ({ signal }) => {
      if (parentSessionId === null) throw new Error("A parent session ID is required.");
      return api.getSession(parentSessionId, signal);
    },
    enabled: parentSessionId !== null,
  });
  const parentSession = parentSessionQuery.data?.id === parentSessionId ? parentSessionQuery.data : null;
  const loadingParentSession = parentSessionId !== null && parentSessionQuery.isLoading;
  const parentSessionLoadError = parentSessionQuery.error === null ? null : errorMessage(parentSessionQuery.error);

  const childSessionsQuery = useInfiniteQuery({
    queryKey: ["ai-session-archive", "children", selectedSession?.id ?? null],
    queryFn: ({ pageParam, signal }) => {
      if (selectedSession === null) throw new Error("A selected session is required.");
      return api.listSessions(
        {
          parentSessionId: selectedSession.id,
          ...(pageParam === null ? {} : { cursor: pageParam }),
          limit: SESSION_PAGE_SIZE,
        },
        signal
      );
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selectedSession !== null,
  });
  const childPages = childSessionsQuery.data?.pages ?? [];
  const childSessions = useMemo(
    () =>
      mergeById(
        [],
        childPages.flatMap((page) => page.data)
      ),
    [childPages]
  );
  const childSessionCursor = childPages.at(-1)?.next_cursor ?? null;
  const loadingChildSessions = selectedSession !== null && childSessionsQuery.isLoading;
  const loadingMoreRelatedSessions = childSessionsQuery.isFetchingNextPage;
  const childSessionsLoadError = childSessionsQuery.error === null ? null : errorMessage(childSessionsQuery.error);
  const error =
    [machinesQuery.error, sessionsQuery.error, versionsQuery.error, eventQuery.error].find(
      (reason): reason is Error => reason !== null
    ) ?? null;
  const errorText = error === null ? null : errorMessage(error);
  const loadingMachines = machinesQuery.isLoading;

  // Scroll and layout callbacks can call these again before a render publishes isFetchingNextPage.
  // cancelRefetch: false joins the in-flight page; the default would abort it and refetch.
  const loadMoreSessions = (): void => {
    if (sessionsQuery.hasNextPage) void sessionsQuery.fetchNextPage({ cancelRefetch: false });
  };

  const loadEarlierEvents = (): void => {
    if (eventQuery.hasNextPage) void eventQuery.fetchNextPage({ cancelRefetch: false });
  };

  const loadMoreRelatedSessions = (): void => {
    if (childSessionsQuery.hasNextPage) void childSessionsQuery.fetchNextPage({ cancelRefetch: false });
  };

  const refreshSessionList = (): void => {
    void queryClient.resetQueries({ queryKey: sessionListQueryKey, exact: true });
  };

  return {
    machines,
    loadingMachines,
    sessions,
    sessionCursor,
    loadingSessions,
    loadingMoreSessions,
    loadedSessionScope,
    selectedSession,
    loadingSelectedSession,
    selectedSessionError,
    versions,
    requestedVersionUnavailable,
    selectedVersionId,
    selectedVersion,
    loadingVersions,
    events,
    eventCursor,
    loadingEvents,
    loadingEarlierEvents,
    eventScope,
    parentSession,
    loadingParentSession,
    parentSessionLoadError,
    childSessions,
    childSessionCursor,
    loadingChildSessions,
    loadingMoreRelatedSessions,
    childSessionsLoadError,
    hasMoreChildSessions: childSessionsQuery.hasNextPage,
    errorText,
    loadMoreSessions,
    loadEarlierEvents,
    loadMoreRelatedSessions,
    refreshSessionList,
  };
}

function mergeById<T extends { id: string }>(previous: T[], incoming: T[]): T[] {
  const byId = new Map(previous.map((item) => [item.id, item]));
  for (const item of incoming) byId.set(item.id, item);
  return [...byId.values()];
}

function mergeByOrdinal(previous: EventRecord[], incoming: EventRecord[]): EventRecord[] {
  const byOrdinal = new Map(previous.map((event) => [event.ordinal, event]));
  for (const event of incoming) byOrdinal.set(event.ordinal, event);
  return [...byOrdinal.values()].sort((left, right) => left.ordinal - right.ordinal);
}

function errorMessage(reason: unknown, fallback = "Could not load local sessions."): string {
  return reason instanceof Error ? reason.message : fallback;
}
